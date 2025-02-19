from typing import Any, List, Optional, Tuple, cast

import tensorflow as tf
import tensorflow_addons as tfa
from opennmt import END_OF_SENTENCE_ID, START_OF_SENTENCE_ID, UNKNOWN_TOKEN
from opennmt.data.vocab import get_mapping, update_variable, update_variable_and_slots
from opennmt.encoders import ParallelEncoder
from opennmt.inputters import ParallelInputter, WordEmbedder, add_sequence_controls
from opennmt.layers import MultiHeadAttentionReduction, SinusoidalPositionEncoder
from opennmt.layers.reducer import align_in_time
from opennmt.models import (
    EmbeddingsSharingLevel,
    SequenceToSequence,
    SequenceToSequenceInputter,
    Transformer,
    register_model_in_catalog,
)
from opennmt.models.sequence_to_sequence import _add_noise, replace_unknown_target
from opennmt.utils import BeamSearch, DecodingStrategy, Sampler
from opennmt.utils.misc import shape_list

from .decoding import DictionaryGuidedBeamSearch
from .sil_self_attention_decoder import SILSelfAttentionDecoder
from .sil_self_attention_encoder import SILSelfAttentionEncoder
from .sil_source_word_embedder import SILSourceWordEmbedder
from .trie import Trie, TrieCompiler


class SILTransformer(Transformer):
    def __init__(
        self,
        source_inputter=None,
        target_inputter=None,
        num_layers=6,
        num_units=512,
        num_heads=8,
        ffn_inner_dim=2048,
        dropout=0.1,
        attention_dropout=0.1,
        ffn_dropout=0.1,
        ffn_activation=tf.nn.relu,
        mha_bias=True,
        position_encoder_class=SinusoidalPositionEncoder,
        share_embeddings=EmbeddingsSharingLevel.NONE,
        share_encoders=False,
        maximum_relative_position=None,
        attention_reduction=MultiHeadAttentionReduction.FIRST_HEAD_LAST_LAYER,
        pre_norm=True,
        output_layer_bias=True,
        drop_encoder_self_attention_residual_connections=set(),
        alignment_head_num_units=None,
    ):
        if source_inputter is None:
            source_inputter = SILSourceWordEmbedder(embedding_size=num_units)
        if target_inputter is None:
            target_inputter = WordEmbedder(embedding_size=num_units)

        if isinstance(num_layers, (list, tuple)):
            num_encoder_layers, num_decoder_layers = num_layers
        else:
            num_encoder_layers, num_decoder_layers = num_layers, num_layers
        encoders = [
            SILSelfAttentionEncoder(
                num_encoder_layers,
                num_units=num_units,
                num_heads=num_heads,
                ffn_inner_dim=ffn_inner_dim,
                dropout=dropout,
                attention_dropout=attention_dropout,
                ffn_dropout=ffn_dropout,
                ffn_activation=ffn_activation,
                mha_bias=mha_bias,
                position_encoder_class=position_encoder_class,
                maximum_relative_position=maximum_relative_position,
                pre_norm=pre_norm,
                drop_self_attention_residual_connections=drop_encoder_self_attention_residual_connections,
            )
            for _ in range(source_inputter.num_outputs)
        ]
        if len(encoders) > 1:
            encoder = ParallelEncoder(
                encoders if not share_encoders else encoders[0],
                outputs_reducer=None,
                states_reducer=None,
            )
        else:
            encoder = encoders[0]
        decoder = SILSelfAttentionDecoder(
            num_decoder_layers,
            num_units=num_units,
            num_heads=num_heads,
            ffn_inner_dim=ffn_inner_dim,
            dropout=dropout,
            attention_dropout=attention_dropout,
            ffn_dropout=ffn_dropout,
            ffn_activation=ffn_activation,
            mha_bias=mha_bias,
            position_encoder_class=position_encoder_class,
            num_sources=source_inputter.num_outputs,
            maximum_relative_position=maximum_relative_position,
            attention_reduction=attention_reduction,
            pre_norm=pre_norm,
            output_layer_bias=output_layer_bias,
            alignment_head_num_units=alignment_head_num_units,
        )

        self._pre_norm = pre_norm
        self._num_units = num_units
        self._num_encoder_layers = num_encoder_layers
        self._num_decoder_layers = num_decoder_layers
        self._num_heads = num_heads
        self._with_relative_position = maximum_relative_position is not None
        self._position_encoder_class = position_encoder_class
        self._ffn_activation = ffn_activation
        self._alignment_layer = -1
        self._alignment_heads = 1
        if attention_reduction == MultiHeadAttentionReduction.AVERAGE_LAST_LAYER:
            self._alignment_heads = 0

        self._dictionary: Optional[Trie] = None

        if not isinstance(target_inputter, WordEmbedder):
            raise TypeError("Target inputter must be a WordEmbedder")
        if EmbeddingsSharingLevel.share_input_embeddings(share_embeddings):
            if isinstance(source_inputter, ParallelInputter):
                source_inputters = source_inputter.inputters
            else:
                source_inputters = [source_inputter]
            for inputter in source_inputters:
                if not isinstance(inputter, WordEmbedder):
                    raise TypeError("Sharing embeddings requires all inputters to be a " "WordEmbedder")

        examples_inputter = SILSequenceToSequenceInputter(
            source_inputter,
            target_inputter,
            share_parameters=EmbeddingsSharingLevel.share_input_embeddings(share_embeddings),
        )
        super(SequenceToSequence, self).__init__(examples_inputter)
        self.encoder = encoder
        self.decoder = decoder
        self.share_embeddings = share_embeddings

    def initialize(self, data_config, params=None):
        super().initialize(data_config, params=params)
        src_dict_path: Optional[str] = data_config.get("source_dictionary")
        trg_dict_path: Optional[str] = data_config.get("target_dictionary")
        ref_dict_path: Optional[str] = data_config.get("ref_dictionary")
        if src_dict_path is not None and trg_dict_path is not None and ref_dict_path is not None:
            self.labels_inputter.set_decoder_mode(enable=False, mark_start=False, mark_end=False)
            dictionary_compiler = TrieCompiler(self.features_inputter.vocabulary_size)
            with tf.io.gfile.GFile(src_dict_path) as src_dict, tf.io.gfile.GFile(
                trg_dict_path
            ) as trg_dict, tf.io.gfile.GFile(ref_dict_path) as ref_dict:
                for src_entry_str, trg_entry_str, ref_entry_str in zip(src_dict, trg_dict, ref_dict):
                    src_entry = src_entry_str.strip().split("\t")
                    src_ids = [self.features_inputter.make_features(tf.constant(se.strip()))["ids"] for se in src_entry]
                    trg_entry = trg_entry_str.strip().split("\t")
                    trg_ids = [self.labels_inputter.make_features(tf.constant(te.strip()))["ids"] for te in trg_entry]
                    refs = ref_entry_str.strip().split("\t")
                    for src_variant_ids in src_ids:
                        dictionary_compiler.add(src_variant_ids, trg_ids, refs)
            if not dictionary_compiler.empty:
                self._dictionary = dictionary_compiler.compile()
            self.labels_inputter.set_decoder_mode(mark_start=True, mark_end=True)

        cast(SILSequenceToSequenceInputter, self.examples_inputter).features_has_ref = isinstance(
            data_config["eval_features_file"], list
        )

    def analyze(self, features):
        # Encode the source.
        source_length = self.features_inputter.get_length(features)
        source_inputs = self.features_inputter(features)
        encoder_outputs, encoder_state, encoder_sequence_length = self.encoder(
            source_inputs, sequence_length=source_length
        )

        predictions = self._dynamic_decode(features, encoder_outputs, encoder_state, encoder_sequence_length)

        length = predictions["length"]
        length = tf.squeeze(length, axis=[1])
        tokens = predictions["tokens"]
        tokens = tf.squeeze(tokens, axis=[1])
        tokens = tf.where(tf.equal(tokens, "</s>"), tf.fill(tf.shape(tokens), ""), tokens)

        ids = self.labels_inputter.tokens_to_ids.lookup(tokens)
        if self.labels_inputter.mark_start or self.labels_inputter.mark_end:
            ids, length = add_sequence_controls(
                ids,
                length,
                start_id=START_OF_SENTENCE_ID if self.labels_inputter.mark_start else None,
                end_id=END_OF_SENTENCE_ID if self.labels_inputter.mark_end else None,
            )
        labels = {"ids_out": ids[:, 1:], "ids": ids[:, :-1], "length": length - 1}

        outputs = self._decode_target(labels, encoder_outputs, encoder_state, encoder_sequence_length)

        return {
            "length": tf.squeeze(predictions["length"], axis=[1]),
            "tokens": tf.squeeze(predictions["tokens"], axis=[1]),
            "alignment": tf.squeeze(predictions["alignment"], axis=[1]),
            "encoder_outputs": encoder_outputs,
            "logits": outputs["logits"],
            "index": features["index"],
        }

    def set_dropout(self, dropout: float = 0.1, attention_dropout: float = 0.1, ffn_dropout: float = 0.1) -> None:
        root_layer = self
        for layer in (root_layer,) + root_layer.submodules:
            name: str = layer.name
            if "self_attention_encoder" in name:
                layer.dropout = dropout
            elif "self_attention_decoder" in name:
                layer.dropout = dropout
            elif "transformer_layer_wrapper" in name:
                layer.output_dropout = dropout
            elif name.startswith("multi_head_attention"):
                layer.dropout = attention_dropout
            elif name.startswith("feed_forward_network"):
                layer.dropout = ffn_dropout

    def _dynamic_decode(
        self,
        features,
        encoder_outputs,
        encoder_state,
        encoder_sequence_length,
        tflite_run=False,
    ):
        params = self.params
        batch_size = tf.shape(tf.nest.flatten(encoder_outputs)[0])[0]
        start_ids = tf.fill([batch_size], START_OF_SENTENCE_ID)
        beam_size = params.get("beam_width", 1)

        if beam_size > 1:
            # Tile encoder outputs to prepare for beam search.
            encoder_outputs = tfa.seq2seq.tile_batch(encoder_outputs, beam_size)
            encoder_sequence_length = tfa.seq2seq.tile_batch(encoder_sequence_length, beam_size)
            encoder_state = tf.nest.map_structure(
                lambda state: tfa.seq2seq.tile_batch(state, beam_size) if state is not None else None,
                encoder_state,
            )

        decoding_strategy = DecodingStrategy.from_params(params, tflite_mode=tflite_run)
        if self._dictionary is not None and isinstance(decoding_strategy, BeamSearch):
            src_ids: tf.Tensor = features["ids"]
            ref = tf.RaggedTensor.from_tensor(features["ref"], lengths=features["ref_length"])
            src_entry_indices, trg_entries = self.batch_find_trg_entries(src_ids, ref)
            decoding_strategy = DictionaryGuidedBeamSearch(
                src_entry_indices,
                trg_entries,
                decoding_strategy.beam_size,
                decoding_strategy.length_penalty,
                decoding_strategy.coverage_penalty,
                decoding_strategy.tflite_output_size,
            )

        # Dynamically decodes from the encoder outputs.
        initial_state = self.decoder.initial_state(
            memory=encoder_outputs,
            memory_sequence_length=encoder_sequence_length,
            initial_state=encoder_state,
        )
        (sampled_ids, sampled_length, log_probs, alignment, _,) = self.decoder.dynamic_decode(
            self.labels_inputter,
            start_ids,
            initial_state=initial_state,
            decoding_strategy=decoding_strategy,
            sampler=Sampler.from_params(params),
            maximum_iterations=params.get("maximum_decoding_length", 250),
            minimum_iterations=params.get("minimum_decoding_length", 0),
            tflite_output_size=params.get("tflite_output_size", 250) if tflite_run else None,
        )

        if tflite_run:
            target_tokens = sampled_ids
        else:
            target_tokens = self.labels_inputter.ids_to_tokens.lookup(tf.cast(sampled_ids, tf.int64))
        # Maybe replace unknown targets by the source tokens with the highest attention weight.
        if params.get("replace_unknown_target", False):
            if alignment is None:
                raise TypeError(
                    "replace_unknown_target is not compatible with decoders " "that don't return alignment history"
                )
            if not isinstance(self.features_inputter, WordEmbedder):
                raise TypeError("replace_unknown_target is only defined when the source " "inputter is a WordEmbedder")

            source_tokens = features if tflite_run else features["tokens"]
            if beam_size > 1:
                source_tokens = tfa.seq2seq.tile_batch(source_tokens, beam_size)
            original_shape = tf.shape(target_tokens)
            if tflite_run:
                target_tokens = tf.squeeze(target_tokens, axis=0)
                output_size = original_shape[-1]
                unknown_token = self.labels_inputter.vocabulary_size - 1
            else:
                target_tokens = tf.reshape(target_tokens, [-1, original_shape[-1]])
                output_size = tf.shape(target_tokens)[1]
                unknown_token = UNKNOWN_TOKEN

            align_shape = shape_list(alignment)
            attention = tf.reshape(
                alignment,
                [align_shape[0] * align_shape[1], align_shape[2], align_shape[3]],
            )
            attention = align_in_time(attention, output_size)
            replaced_target_tokens = replace_unknown_target(
                target_tokens, source_tokens, attention, unknown_token=unknown_token
            )
            if tflite_run:
                target_tokens = replaced_target_tokens
            else:
                target_tokens = tf.reshape(replaced_target_tokens, original_shape)

        if tflite_run:
            if beam_size > 1:
                target_tokens = tf.transpose(target_tokens)
                target_tokens = target_tokens[:, :1]
            target_tokens = tf.squeeze(target_tokens)

            return target_tokens
        # Maybe add noise to the predictions.
        decoding_noise = params.get("decoding_noise")
        if decoding_noise:
            target_tokens, sampled_length = _add_noise(
                target_tokens,
                sampled_length,
                decoding_noise,
                params.get("decoding_subword_token", "￭"),
                params.get("decoding_subword_token_is_spacer"),
            )
            alignment = None  # Invalidate alignments.

        predictions = {"log_probs": log_probs}
        if self.labels_inputter.tokenizer.in_graph:
            detokenized_text = self.labels_inputter.tokenizer.detokenize(
                tf.reshape(target_tokens, [batch_size * beam_size, -1]),
                sequence_length=tf.reshape(sampled_length, [batch_size * beam_size]),
            )
            predictions["text"] = tf.reshape(detokenized_text, [batch_size, beam_size])
        else:
            predictions["tokens"] = target_tokens
            predictions["length"] = sampled_length
            if alignment is not None:
                predictions["alignment"] = alignment

        # Maybe restrict the number of returned hypotheses based on the user parameter.
        num_hypotheses = params.get("num_hypotheses", 1)
        if num_hypotheses > 0:
            if num_hypotheses > beam_size:
                raise ValueError("n_best cannot be greater than beam_width")
            for key, value in predictions.items():
                predictions[key] = value[:, :num_hypotheses]
        return predictions

    def batch_find_trg_entries(self, src_ids: tf.Tensor, ref: tf.RaggedTensor) -> Tuple[tf.Tensor, tf.Tensor]:
        if self._dictionary is None:
            raise ValueError("The dictionary must be initialized.")
        ref_id = self._dictionary.get_ref_id(ref)
        src_entry_indices, trg_entries = tf.map_fn(
            lambda args: self.find_trg_entries(args[0], args[1]),
            (src_ids, ref_id),
            fn_output_signature=(
                tf.TensorSpec((None), dtype=tf.int32),
                tf.RaggedTensorSpec(shape=(None, None, None), dtype=tf.int32, row_splits_dtype=tf.int32),
            ),
        )
        return src_entry_indices, trg_entries.to_tensor()

    @tf.function
    def find_trg_entries(self, src_ids: tf.Tensor, ref_id: tf.Tensor) -> Tuple[tf.Tensor, tf.RaggedTensor]:
        if self._dictionary is None:
            raise ValueError("The dictionary must be initialized.")

        if tf.size(ref_id) == 0:
            src_entry_indices = tf.zeros_like(src_ids, dtype=tf.int32)
            trg_entries = tf.ragged.constant(
                [[]], ragged_rank=2, inner_shape=(None), dtype=tf.int32, row_splits_dtype=tf.int32
            )
        else:
            length = tf.shape(src_ids)[0]
            src_entry_indices_array = tf.TensorArray(tf.int32, size=length)
            trg_entry_lengths_array = tf.TensorArray(tf.int32, size=0, dynamic_size=True)
            trg_variants_array = tf.TensorArray(tf.int32, size=0, dynamic_size=True, infer_shape=False)
            trg_variant_lengths_array = tf.TensorArray(tf.int32, size=0, dynamic_size=True)

            trg_entry_lengths_array = trg_entry_lengths_array.write(0, 0)
            i = 0
            j = 1
            k = 0
            while i < length:
                trg_entry, prefix_len = self._dictionary.longest_prefix(src_ids[i:], ref_id)
                if prefix_len == 0:
                    src_entry_indices_array = src_entry_indices_array.write(i, 0)
                    i += 1
                else:
                    num_variants = trg_entry.nrows()
                    trg_entry_lengths_array = trg_entry_lengths_array.write(j, num_variants)
                    end = i + prefix_len
                    while i < end:
                        src_entry_indices_array = src_entry_indices_array.write(i, j)
                        i += 1
                    j += 1
                    for vi in tf.range(num_variants):
                        trg_variant = trg_entry[vi]
                        trg_variants_array = trg_variants_array.write(k, trg_variant)
                        trg_variant_lengths_array = trg_variant_lengths_array.write(k, tf.shape(trg_variant)[0])
                        k += 1
            if k == 0:
                trg_variants_array = trg_variants_array.write(0, tf.constant([], dtype=tf.int32))
            src_entry_indices = src_entry_indices_array.stack()
            trg_entries = tf.RaggedTensor.from_nested_row_lengths(
                trg_variants_array.concat(), [trg_entry_lengths_array.stack(), trg_variant_lengths_array.stack()]
            )
        return src_entry_indices, trg_entries

    def transfer_weights(
        self,
        new_model: "SILTransformer",
        new_optimizer: Any = None,
        optimizer: Any = None,
        ignore_weights: Optional[List[tf.Variable]] = None,
    ):
        updated_variables = []

        def _map_variable(mapping, var_a, var_b, axis=0):
            if new_optimizer is not None and optimizer is not None:
                variables = update_variable_and_slots(
                    var_a,
                    var_b,
                    optimizer,
                    new_optimizer,
                    mapping,
                    vocab_axis=axis,
                )
            else:
                variables = [update_variable(var_a, var_b, mapping, vocab_axis=axis)]
            updated_variables.extend(variables)

        source_mapping, _ = get_mapping(
            self.features_inputter.vocabulary_file,
            new_model.features_inputter.vocabulary_file,
        )
        target_mapping, _ = get_mapping(
            self.labels_inputter.vocabulary_file,
            new_model.labels_inputter.vocabulary_file,
        )

        _map_variable(
            source_mapping,
            self.features_inputter.embedding,
            new_model.features_inputter.embedding,
        )
        _map_variable(
            target_mapping,
            self.decoder.output_layer.bias,
            new_model.decoder.output_layer.bias,
        )

        if not EmbeddingsSharingLevel.share_input_embeddings(self.share_embeddings):
            _map_variable(
                target_mapping,
                self.labels_inputter.embedding,
                new_model.labels_inputter.embedding,
            )
        if not EmbeddingsSharingLevel.share_target_embeddings(self.share_embeddings):
            _map_variable(
                target_mapping,
                self.decoder.output_layer.kernel,
                new_model.decoder.output_layer.kernel,
                axis=1,
            )

        return super(SequenceToSequence, self).transfer_weights(
            new_model,
            new_optimizer=new_optimizer,
            optimizer=optimizer,
            ignore_weights=updated_variables + (ignore_weights if ignore_weights is not None else []),
        )


class SILSequenceToSequenceInputter(SequenceToSequenceInputter):
    def __init__(self, features_inputter, labels_inputter, share_parameters=False):
        super().__init__(features_inputter, labels_inputter, share_parameters)
        self.features_has_ref = False

    def _structure(self):
        structure = []
        if isinstance(self.features_inputter, SILSourceWordEmbedder):
            structure.append([None, None] if self.features_has_ref else None)
        elif isinstance(self.features_inputter, ParallelInputter):
            structure.append(self.features_inputter._structure())
        else:
            structure.append(None)
        structure.append(None)
        return structure


@register_model_in_catalog
class SILTransformerMedium(SILTransformer):
    def __init__(self):
        super().__init__(num_layers=3)


@register_model_in_catalog(alias="SILTransformer")
class SILTransformerBase(SILTransformer):
    """Defines a Transformer model as decribed in https://arxiv.org/abs/1706.03762."""


@register_model_in_catalog
class SILTransformerBaseAlignmentEnhanced(SILTransformer):
    """Defines a Transformer model as decribed in https://arxiv.org/abs/1706.03762."""

    def __init__(self):
        super().__init__(
            attention_reduction=MultiHeadAttentionReduction.AVERAGE_ALL_LAYERS, alignment_head_num_units=64
        )


@register_model_in_catalog(alias="SILTransformerRelative")
class SILTransformerBaseRelative(SILTransformer):
    """
    Defines a Transformer model using relative position representations as described in
    https://arxiv.org/abs/1803.02155.
    """

    def __init__(self):
        super().__init__(position_encoder_class=None, maximum_relative_position=20)


@register_model_in_catalog
class SILTransformerBaseNoResidual(SILTransformer):
    """
    Defines a Transformer model with no residual connection for the self-attention layer of a middle encoder layer
    as described in https://arxiv.org/abs/2012.15127.
    """

    def __init__(self):
        super().__init__(drop_encoder_self_attention_residual_connections={3})


@register_model_in_catalog
class SILTransformerBig(SILTransformer):
    """Defines a large Transformer model as decribed in https://arxiv.org/abs/1706.03762."""

    def __init__(self):
        super().__init__(num_units=1024, num_heads=16, ffn_inner_dim=4096)


@register_model_in_catalog
class SILTransformerBigRelative(SILTransformer):
    """
    Defines a large Transformer model using relative position representations as described in
    https://arxiv.org/abs/1803.02155.
    """

    def __init__(self):
        super().__init__(
            num_units=1024, num_heads=16, ffn_inner_dim=4096, position_encoder_class=None, maximum_relative_position=20
        )


@register_model_in_catalog
class SILTransformerTiny(SILTransformer):
    """Defines a tiny Transformer model."""

    def __init__(self):
        super().__init__(num_layers=2, num_units=64, num_heads=2, ffn_inner_dim=64)
