"""Semantic search over the document library.

Four small pieces, each testable on its own:

    chunk.py   text  -> chunks          (pure; the strategy lives here)
    embed.py   text  -> vectors         (one HTTP call to an OpenAI-compatible
                                         /embeddings endpoint)
    store.py   vectors <-> SQLite       (packing, cosine, the index's state)
    index.py   a document -> its rows   (the job the worker runs)

`worker.py` is the only thing here that owns a thread; everything else is a
function you can call from a test.
"""
