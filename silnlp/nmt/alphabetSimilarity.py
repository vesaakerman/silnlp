import argparse
import os
from glob import glob
from typing import Iterable, Iterator, List, Set

from nlp.common.environment import paratextPreprocessedDir
from nlp.nmt.config import get_root_dir, load_config

import pandas as pd

import matplotlib.pyplot as plt
import seaborn as sns


def computeSimilarity(file_names: List[str]) -> None:
    charSets = list()
    file_names.sort()
    for file_name in file_names:
        file_path = os.path.join(paratextPreprocessedDir, "data", f"{file_name}.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        thisCharSet = list()
        for i in range(len(text)):
            if text[i] not in thisCharSet:
                thisCharSet.append(text[i])
        thisCharSet.sort()
        print(f"Resource: {file_name:12}\t#chars: {len(thisCharSet)}")
        charSets.append({"resource": file_name, "chars": thisCharSet})

    # Create a matrix for storing the similarity metrics
    similarityDf = pd.DataFrame(columns=[file_names])
    similarityDf.reindex(file_names)

    totalSimilarity = 0.0
    for i in range(len(file_names) - 1):
        # Get the charset for resource 1 and make a set from it
        file_name1 = file_names[i]
        charSet1 = charSets[i]
        l1 = charSet1.get("chars")
        l1set = set(l1)

        for j in range(i + 1, len(file_names)):
            # Get the charset for resource 2 and make a set from it.
            file_name2 = file_names[j]
            charSet2 = charSets[j]
            l2 = charSet2.get("chars")
            l2set = set(l2)

            # Calculate the differences between sets 1 and 2
            diff1v2 = l1set.difference(l2set)
            diff2v1 = l2set.difference(l1set)

            # Calculate the similarity score
            similarity = (1 - (len(diff1v2) / len(l1))) * (1 - (len(diff2v1) / len(l2))) * 100
            totalSimilarity += similarity
            print(f"Similarity ({file_name1:12} vs {file_name2:12}):\t{similarity:5.1f}")

            # Update similarity score in the dataframe
            similarityDf.at[file_name1, file_name2] = similarity
            similarityDf.at[file_name2, file_name1] = similarity

    totalSimilarity = totalSimilarity / (len(file_names) * (len(file_names) - 1) / 2)
    print(f"Overall similarity: {totalSimilarity:5.1f}")

    # Show the scores as a heatmap
    similarityDf.fillna(0, inplace=True)
    fig, ax = plt.subplots(figsize=(6, 6))
    sns.heatmap(similarityDf, square=True, ax=ax)
    plt.show()


def get_iso(file_path: str) -> str:
    file_name = os.path.splitext(os.path.basename(file_path))[0]
    parts = file_name.split("-")
    return parts[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculates alphabet similarity between text corpora in a multilingual data set"
    )
    parser.add_argument("task", help="Task name")
    args = parser.parse_args()

    task_name = args.task
    root_dir = get_root_dir(task_name)
    config = load_config(task_name)
    data_config: dict = config.get("data", {})

    src_langs: Set[str] = set(data_config.get("src_langs", []))
    trg_langs: Set[str] = set(data_config.get("trg_langs", []))
    write_trg_token = len(trg_langs) > 1

    src_file_names: List[str] = list()
    trg_file_names: List[str] = list()
    for file_path in glob(os.path.join(paratextPreprocessedDir, "data", "*.txt")):
        iso = get_iso(file_path)
        if iso in src_langs:
            file_name = os.path.splitext(os.path.basename(file_path))[0]
            src_file_names.append(file_name)
        if iso in trg_langs:
            file_name = os.path.splitext(os.path.basename(file_path))[0]
            trg_file_names.append(file_name)

    src_file_names.sort()
    trg_file_names.sort()

    computeSimilarity(list(set().union(src_file_names, trg_file_names)))


if __name__ == "__main__":
    main()
