"""File-type extractors and the extractor registry. (M1)

Each extractor turns a file into plain text (plus optional page markers). The
registry dispatches by extension; unknown types fall back to a generic extractor.
"""
