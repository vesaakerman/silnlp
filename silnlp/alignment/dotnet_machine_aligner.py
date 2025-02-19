import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..common.utils import check_dotnet, get_repo_dir
from ..common.environment import get_env_path
from .aligner import Aligner
from .lexicon import Lexicon

LOGGER = logging.getLogger(__name__)


class DotnetMachineAligner(Aligner):
    def __init__(
        self,
        id: str,
        model_type: str,
        model_dir: Path,
        plugin_file_path: Optional[Path] = None,
        has_inverse_model: bool = True,
        threshold: float = 0.01,
        direct_model_prefix: str = "src_trg_invswm",
        params: Dict[str, Any] = {},
    ) -> None:
        super().__init__(id, model_dir)
        self.model_type = model_type
        self._plugin_file_path = plugin_file_path
        self._has_inverse_model = has_inverse_model
        self._threshold = threshold
        self._direct_model_prefix = direct_model_prefix
        self._params = params
        self._lowercase = False

    @property
    def has_inverse_model(self) -> bool:
        return self._has_inverse_model

    @property
    def lowercase(self) -> bool:
        return self._lowercase

    @lowercase.setter
    def lowercase(self, value: bool) -> None:
        self._lowercase = value

    def train(self, src_file_path: Path, trg_file_path: Path) -> None:
        direct_lex_path = self.model_dir / "lexicon.direct.txt"
        if direct_lex_path.is_file():
            direct_lex_path.unlink()
        inverse_lex_path = self.model_dir / "lexicon.inverse.txt"
        if inverse_lex_path.is_file():
            inverse_lex_path.unlink()
        self.model_dir.mkdir(exist_ok=True)
        if self.model_type == "ibm4":
            self._execute_mkcls(src_file_path, "src")
            self._execute_mkcls(trg_file_path, "trg")
        self._train_alignment_model(src_file_path, trg_file_path)

    def align(self, out_file_path: Path, sym_heuristic: str = "grow-diag-final-and") -> None:
        self._align_parallel_corpus(
            self.model_dir / "src_trg_invswm.src", self.model_dir / "src_trg_invswm.trg", out_file_path, sym_heuristic
        )

    def force_align(
        self, src_file_path: Path, trg_file_path: Path, out_file_path: Path, sym_heuristic: str = "grow-diag-final-and"
    ) -> None:
        self._align_parallel_corpus(src_file_path, trg_file_path, out_file_path, sym_heuristic)

    def extract_lexicon(self, out_file_path: Path) -> None:
        lexicon = self.get_direct_lexicon()
        if self._has_inverse_model:
            inverse_lexicon = self.get_inverse_lexicon()
            print("Symmetrizing lexicons...", end="", flush=True)
            lexicon = Lexicon.symmetrize(lexicon, inverse_lexicon)
            print(" done.")
        lexicon.write(out_file_path)

    def get_direct_lexicon(self, include_special_tokens: bool = False) -> Lexicon:
        direct_lex_path = self.model_dir / "lexicon.direct.txt"
        self._extract_lexicon("direct", direct_lex_path)
        return Lexicon.load(direct_lex_path, include_special_tokens)

    def get_inverse_lexicon(self, include_special_tokens: bool = False) -> Lexicon:
        if not self._has_inverse_model:
            raise RuntimeError("The aligner does not have an inverse model.")
        inverse_lex_path = self.model_dir / "lexicon.inverse.txt"
        self._extract_lexicon("inverse", inverse_lex_path)
        return Lexicon.load(inverse_lex_path, include_special_tokens)

    def _train_alignment_model(self, src_file_path: Path, trg_file_path: Path) -> None:
        check_dotnet()
        ibm2_iter_count = 5 if self.model_type == "ibm2" else 0
        args: List[str] = [
            "dotnet",
            "machine",
            "train",
            "alignment-model",
            str(self.model_dir) + os.sep,
            str(src_file_path),
            str(trg_file_path),
            "-mt",
            self.model_type,
            "-tp",
            "ibm1-iters=5",
            "-tp",
            f"ibm2-iters={ibm2_iter_count}",
            "-tp",
            "hmm-iters=5",
            "-tp",
            "ibm3-iters=5",
            "-tp",
            "ibm4-iters=5",
        ]
        if self.model_type == "ibm4":
            src_classes_path = self.model_dir / f"src_trg.src.classes"
            args.extend(["-tp", f"src-classes={src_classes_path}"])
            trg_classes_path = self.model_dir / f"src_trg.trg.classes"
            args.extend(["-tp", f"trg-classes={trg_classes_path}"])
        if self._lowercase:
            args.append("-l")
        if self._plugin_file_path is not None:
            args.append("-mp")
            args.append(str(self._plugin_file_path))
        if len(self._params) > 0:
            args.append("-tp")
            for key, value in self._params.items():
                args.append(f"{key}={value}")
        subprocess.run(args, cwd=get_repo_dir())

    def _align_parallel_corpus(
        self, src_file_path: Path, trg_file_path: Path, output_file_path: Path, sym_heuristic: str
    ) -> None:
        check_dotnet()
        args: List[str] = [
            "dotnet",
            "machine",
            "align",
            str(self.model_dir) + os.sep,
            str(src_file_path),
            str(trg_file_path),
            str(output_file_path),
            "-sh",
            sym_heuristic,
        ]
        if self._lowercase:
            args.append("-l")
        if self._plugin_file_path is not None:
            args.append("-mp")
            args.append(str(self._plugin_file_path))
        subprocess.run(args, cwd=get_repo_dir())

    def _extract_lexicon(self, direction: str, out_file_path: Path) -> None:
        check_dotnet()
        args: List[str] = [
            "dotnet",
            "machine",
            "extract-lexicon",
            str(self.model_dir),
            str(out_file_path),
            "-mt",
            self.model_type,
            "-p",
            "-ss",
            "-t",
            str(self._threshold),
            "-d",
            direction,
        ]
        if self._plugin_file_path is not None:
            args.append("-mp")
            args.append(str(self._plugin_file_path))
        subprocess.run(args, cwd=get_repo_dir())

    def _execute_mkcls(self, input_file_path: Path, side: str) -> None:
        mkcls_path = Path(get_env_path("MGIZA_PATH"), "mkcls")
        if platform.system() == "Windows":
            mkcls_path = mkcls_path.with_suffix(".exe")
        if not mkcls_path.is_file():
            raise RuntimeError("mkcls is not installed.")

        output_file_path = self.model_dir / f"src_trg.{side}.classes"

        args: List[str] = [str(mkcls_path), "-n10", f"-p{input_file_path}", f"-V{output_file_path}"]
        subprocess.run(args)


class Ibm1DotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("ibm1", "ibm1", model_dir)


class Ibm2DotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("ibm2", "ibm2", model_dir)


class HmmDotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("hmm", "hmm", model_dir)


class Ibm3DotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("ibm3", "ibm3", model_dir)


class Ibm4DotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("ibm4", "ibm4", model_dir)


class FastAlignDotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__("fast_align", "fast_align", model_dir)


class ParatextDotnetMachineAligner(DotnetMachineAligner):
    def __init__(self, model_dir: Path) -> None:
        super().__init__(
            "pt",
            "betainv",
            model_dir,
            plugin_file_path=Path(get_env_path("BETA_INV_PLUGIN_PATH")),
            has_inverse_model=False,
            threshold=0,
            direct_model_prefix="src_trg",
        )
