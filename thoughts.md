## Goal
a single tool that can search all files in a local directory
run locally on mac, can be used by claude code and codex
support both sparse retrieval and dense retrieval, and knowledge base search
allow agents to call different search functions separately or together
support all types of files, pdf, ppt, docx, and more
Can index the actual content of all files




## Index part
given a directory, index all files without interrupting the original files or directory structure
Check about which files have been updated every day, do incremental or update index
need to split long docs into small passages for indexing

## Query part
support both short and long queries
Support the calling from a coding agent
support query expansion like pseudo relevance feedback if needed  

## System part
Do not put everything in computer memory, keep cpu and memory usage as low as possible

## Vector Database
Zvec: https://github.com/alibaba/zvec

## Sparse Search Engines
PyTerrior: https://github.com/terrier-org/pyterrier
PyGalago: https://github.com/QingyaoAi/PyGalago
PySerini: https://github.com/castorini/pyserini

## Dense Model
Harrier: https://huggingface.co/microsoft/harrier-oss-v1-0.6b
Qwen: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B

## Graph Knowledge base
Obsidian cli