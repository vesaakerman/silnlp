import itertools
import logging
import os
import re
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Set
from xml.sax.saxutils import escape

from lxml import etree

from .canon import book_id_to_number
from .corpus import get_terms_glosses_path, get_terms_metadata_path, get_terms_vrefs_path, load_corpus
from .environment import SIL_NLP_ENV
from .utils import get_repo_dir
from .verse_ref import VerseRef

_TERMS_LISTS = {
    "Major": "BiblicalTerms.xml",
    "All": "AllBiblicalTerms.xml",
    "SilNt": "BiblicalTermsSILNT.xml",
    "Pt6": "BiblicalTermsP6NT.xml",
    "Project": "ProjectBiblicalTerms.xml",
}

LOGGER = logging.getLogger(__name__)


def get_project_dir(project: str) -> Path:
    return SIL_NLP_ENV.pt_projects_dir / project


def get_iso(settings_tree: etree.ElementTree) -> str:
    iso = settings_tree.getroot().findtext("LanguageIsoCode")
    assert iso is not None
    index = iso.index(":")
    return iso[:index]


def extract_project(project: str, include_texts: str, exclude_texts: str, include_markers: bool) -> Path:
    project_dir = get_project_dir(project)
    settings_tree = etree.parse(str(project_dir / "Settings.xml"))
    iso = get_iso(settings_tree)

    ref_dir = SIL_NLP_ENV.assets_dir / "Ref"
    args: List[str] = [
        "dotnet",
        "machine",
        "build-corpus",
        str(ref_dir),
        str(project_dir),
        "-sf",
        "pt",
        "-tf",
        "pt_m" if include_markers else "pt",
        "-as",
        "-ie",
        "-md",
    ]
    output_basename = f"{iso}-{project}"
    if len(include_texts) > 0 or len(exclude_texts) > 0:
        output_basename += "_"
    if len(include_texts) > 0:
        args.append("-i")
        args.append(include_texts)
        for text in include_texts.split(","):
            text = text.strip("*")
            output_basename += f"+{text}"
    if len(exclude_texts) > 0:
        args.append("-e")
        args.append(exclude_texts)
        for text in exclude_texts.split(","):
            text = text.strip("*")
            output_basename += f"-{text}"

    if include_markers:
        output_basename += "-m"

    args.append("-to")
    output_filename = SIL_NLP_ENV.mt_scripture_dir / f"{output_basename}.txt"
    args.append(str(output_filename))

    result = subprocess.run(args, cwd=get_repo_dir(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    if len(result.stderr) > 0:
        raise RuntimeError(result.stderr.decode("utf-8"))

    # check if the number of lines in the file is correct (the same as vref.txt - 31104 ending at REV 22:21)
    segment_count = sum(1 for _ in load_corpus(output_filename))
    LOGGER.info(f"# of Segments: {segment_count}")
    if segment_count != 31104:
        LOGGER.error(f"The number of segments is {segment_count}, but should be 31104 (number of verses in the Bible).")
    return output_filename


def escape_id(id: str) -> str:
    return escape(id).replace("\n", "&#xA;")


def strip_parens(term_str: str) -> str:
    parens: int = 0
    end: int = -1
    for i in reversed(range(len(term_str))):
        c = term_str[i]
        if c == ")":
            if parens == 0:
                end = i + 1
            parens += 1
        elif c == "(":
            parens -= 1
            if parens == 0:
                term_str = term_str[:i] + term_str[end:]
                end = -1
    return term_str


def clean_term(term_str: str) -> str:
    term_str = term_str.strip()
    term_str = strip_parens(term_str)
    return " ".join(term_str.split())


def extract_terms_list(list_type: str, project: Optional[str] = None) -> Dict[str, List[VerseRef]]:
    list_file_name = _TERMS_LISTS.get(list_type)
    if list_file_name is None:
        return {}

    list_name = list_type
    if project is not None:
        list_name = project

    dir = SIL_NLP_ENV.pt_terms_dir if project is None else SIL_NLP_ENV.pt_projects_dir / project
    terms_xml_path = dir / list_file_name

    terms_metadata_path = get_terms_metadata_path(list_name)
    terms_glosses_path = get_terms_glosses_path(list_name)
    terms_vrefs_path = get_terms_vrefs_path(list_name)

    references: Dict[str, List[VerseRef]] = {}
    with open(terms_metadata_path, "w", encoding="utf-8", newline="\n") as terms_metadata_file, open(
        terms_glosses_path, "w", encoding="utf-8", newline="\n"
    ) as terms_glosses_file, open(terms_vrefs_path, "w", encoding="utf-8", newline="\n") as terms_vrefs_file:
        terms_tree = etree.parse(str(terms_xml_path))
        for term_elem in terms_tree.getroot().findall("Term"):
            id = term_elem.get("Id")
            if id is None:
                continue
            id = escape_id(id)
            cat = term_elem.findtext("Category", "?")
            if cat == "":
                cat = "?"
            domain = term_elem.findtext("Domain", "?")
            if domain == "":
                domain = "?"
            gloss_str = term_elem.findtext("Gloss", "")
            match = re.match(r"\[(.+?)\]", gloss_str)
            if match is not None:
                gloss_str = match.group(1)

            refs_elem = term_elem.find("References")
            refs_list: List[VerseRef] = []
            if refs_elem is not None:
                for verse_elem in refs_elem.findall("Verse"):
                    bbbcccvvv = int(verse_elem.text[:9])
                    refs_list.append(VerseRef.from_bbbcccvvv(bbbcccvvv))
                references[id] = refs_list
            gloss_str = gloss_str.replace("?", "")
            gloss_str = clean_term(gloss_str)
            gloss_str = re.sub(r"\s+\d+(\.\d+)*$", "", gloss_str)
            glosses: List[str] = re.split("[;,/]", gloss_str)
            glosses = [gloss.strip() for gloss in glosses if gloss.strip() != ""]
            terms_metadata_file.write(f"{id}\t{cat}\t{domain}\n")
            terms_glosses_file.write("\t".join(glosses) + "\n")
            terms_vrefs_file.write("\t".join(str(vref) for vref in refs_list) + "\n")
    return references


def extract_terms_list_from_renderings(project: str, renderings_tree: etree.ElementTree) -> None:
    terms_metadata_path = get_terms_metadata_path(project)
    with open(terms_metadata_path, "w", encoding="utf-8", newline="\n") as terms_metadata_file:
        for rendering_elem in renderings_tree.getroot().findall("TermRendering"):
            id = rendering_elem.get("Id")
            if id is None:
                continue
            id = escape_id(id)
            if rendering_elem.get("Guess") != "false" or rendering_elem.findtext("Renderings", "") == "":
                continue

            terms_metadata_file.write(f"{id}\t?\t?\n")


def extract_term_renderings(project_folder: str, corpus_filename: Path) -> None:
    project_dir = get_project_dir(project_folder)
    renderings_path = project_dir / "TermRenderings.xml"
    if not renderings_path.is_file():
        return

    renderings_tree = etree.parse(str(renderings_path))
    rendering_elems: Dict[str, etree.Element] = {}
    for elem in renderings_tree.getroot().findall("TermRendering"):
        id = elem.get("Id")
        if id is None:
            continue
        id = escape_id(id)
        rendering_elems[id] = elem

    settings_tree = etree.parse(str(project_dir / "Settings.xml"))
    iso = get_iso(settings_tree)
    project_name = settings_tree.getroot().findtext("Name", project_folder)
    terms_setting = settings_tree.getroot().findtext("BiblicalTermsListSetting", "Major::BiblicalTerms.xml")

    list_type, terms_project, _ = terms_setting.split(":", maxsplit=3)
    list_name = list_type
    references: Dict[str, List[VerseRef]] = {}
    if list_type == "Project":
        if terms_project == project_name:
            references = extract_terms_list(list_type, project_folder)
        else:
            extract_terms_list_from_renderings(project_folder, renderings_tree)
        list_name = project_folder

    corpus: Dict[VerseRef, str] = {}
    if len(references) > 0:
        prev_verse_str = ""
        for ref_str, verse_str in zip(load_corpus(SIL_NLP_ENV.assets_dir / "vref.txt"), load_corpus(corpus_filename)):
            if verse_str == "<range>":
                verse_str = prev_verse_str
            corpus[VerseRef.from_string(ref_str)] = verse_str
            prev_verse_str = verse_str

    terms_metadata_path = get_terms_metadata_path(list_name)
    terms_renderings_path = SIL_NLP_ENV.mt_terms_dir / f"{iso}-{project_folder}-{list_type}-renderings.txt"
    count = 0
    with open(terms_renderings_path, "w", encoding="utf-8", newline="\n") as terms_renderings_file:
        for line in load_corpus(terms_metadata_path):
            id, _, _ = line.split("\t", maxsplit=3)
            rendering_elem = rendering_elems.get(id)
            refs_list = references.get(id, [])

            renderings: Set[str] = set()
            if rendering_elem is not None and rendering_elem.get("Guess", "false") == "false":
                renderings_str = rendering_elem.findtext("Renderings", "")
                if renderings_str != "":
                    for rendering in renderings_str.strip().split("||"):
                        rendering = clean_term(rendering).strip()
                        if len(refs_list) > 0 and "*" in rendering:
                            regex = (
                                re.escape(rendering).replace("\\ \\*\\*\\ ", "(?:\\ \\w+)*\\ ").replace("\\*", "\\w*")
                            )
                            for ref in refs_list:
                                verse_str = corpus.get(ref, "")
                                for match in re.finditer(regex, verse_str):
                                    surface_form = match.group()
                                    renderings.add(surface_form)

                        else:
                            rendering = rendering.replace("*", "").strip()
                            if rendering != "":
                                renderings.add(rendering)
            terms_renderings_file.write("\t".join(renderings) + "\n")
            if len(renderings) > 0:
                count += 1
    if count == 0:
        terms_renderings_path.unlink()
        if list_type == "Project":
            terms_metadata_path.unlink()
            terms_glosses_path = get_terms_glosses_path(list_name)
            if terms_glosses_path.is_file():
                terms_glosses_path.unlink()
    LOGGER.info(f"# of Terms: {count}")


def book_file_name_digits(book_num: int) -> str:
    if book_num < 10:
        return f"0{book_num}"
    if book_num < 40:
        return str(book_num)
    if book_num < 100:
        return str(book_num + 1)
    if book_num < 110:
        return f"A{book_num - 100}"
    if book_num < 120:
        return f"B{book_num - 110}"
    return f"C{book_num - 120}"


def get_book_path(project: str, book: str) -> Path:
    project_dir = get_project_dir(project)
    settings_tree = etree.parse(os.path.join(project_dir, "Settings.xml"))
    naming_elem = settings_tree.find("Naming")
    assert naming_elem is not None

    pre_part = naming_elem.get("PrePart", "")
    post_part = naming_elem.get("PostPart", "")
    book_name_form = naming_elem.get("BookNameForm")
    assert book_name_form is not None

    book_num = book_id_to_number(book)
    if book_name_form == "MAT":
        book_name = book
    elif book_name_form == "40" or book_name_form == "41":
        book_name = book_file_name_digits(book_num)
    else:
        book_name = f"{book_file_name_digits(book_num)}{book}"

    book_file_name = f"{pre_part}{book_name}{post_part}"

    return SIL_NLP_ENV.pt_projects_dir / project / book_file_name
