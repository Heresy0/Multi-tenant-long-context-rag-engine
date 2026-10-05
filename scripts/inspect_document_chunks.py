"""Read-only local parser inspection; no embeddings, model API, or DB writes."""
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.documents.chunk_quality import summarize_quality  # noqa: E402
from backend.app.knowledge.rag import CHUNKING_VERSION, split_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect local chunks without indexing or model calls.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--show-content", action="store_true", help="Include internal document text in stdout.")
    args = parser.parse_args()
    chunks = split_file(args.file)
    rows = []
    for index, chunk in enumerate(chunks):
        keys = ("section_path", "chunk_type", "evidence_role", "parser", "parent_key",
                "page_start", "page_end", "cleaning_version", "quality", "ocr_corrections")
        row = {key: chunk.metadata.get(key) for key in keys}
        row.update(index=index, characters=len(chunk.page_content))
        if args.show_content:
            row["content"] = chunk.page_content
        rows.append(row)
    print(json.dumps({"file_name": args.file.name, "chunking_version": CHUNKING_VERSION,
                      "chunk_count": len(chunks), "quality": summarize_quality(chunks),
                      "chunks": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
