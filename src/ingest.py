"""OFFLINE STEP (run once): load -> chunk by contract section -> embed -> store in Pinecone.
    python -m src.ingest
Two namespaces in one index: 'contracts' (15 vendor agreements) and 'policies' (5 plant policies).
"""
import json
import time

import yaml
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_pinecone import PineconeVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from pinecone import Pinecone, ServerlessSpec

from src import config


def load(folder):
    docs = []
    for path in sorted(folder.glob("*.md")):
        _, front, body = path.read_text().split("---", 2)
        meta = {k: str(v) for k, v in yaml.safe_load(front).items()}
        body = "\n".join(l for l in body.splitlines() if not l.startswith("> Fictional"))
        docs.append(Document(page_content=body.strip(), metadata=meta))
    return docs


def chunk(docs):
    """One chunk per section (a clause), so a search returns the whole clause. Long sections are cut at 900 chars."""
    by_heading = MarkdownHeaderTextSplitter([("#", "h1"), ("##", "h2")], strip_headers=False)
    by_size = RecursiveCharacterTextSplitter(chunk_size=config.CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)
    out = []
    for doc in docs:
        who = doc.metadata.get("vendor_name") or doc.metadata.get("plant_id")
        for s, sec in enumerate(by_heading.split_text(doc.page_content)):
            heading = sec.metadata.get("h2") or sec.metadata.get("h1") or ""
            for p, piece in enumerate(by_size.split_text(sec.page_content)):
                meta = {**doc.metadata, "section": heading, "chunk_id": f"{doc.metadata['doc_id']}#{s}.{p}"}
                out.append(Document(page_content=f"[{doc.metadata['doc_id']} | {who} | {heading}]\n{piece}", metadata=meta))
    return out


def main():
    if not config.PINECONE_API_KEY:
        raise SystemExit("Add PINECONE_API_KEY to .env first.")
    contracts, policies = chunk(load(config.CONTRACTS_DIR)), chunk(load(config.POLICIES_DIR))
    print(f"{len(contracts)} contract chunks, {len(policies)} policy chunks")
    pc = Pinecone(api_key=config.PINECONE_API_KEY)
    if config.PINECONE_INDEX not in pc.list_indexes().names():
        print(f"Creating index {config.PINECONE_INDEX} (384 dims, cosine)...")
        pc.create_index(name=config.PINECONE_INDEX, dimension=config.EMBED_DIM, metric="cosine",
                        spec=ServerlessSpec(cloud="aws", region="us-east-1"))
        while not pc.describe_index(config.PINECONE_INDEX).status["ready"]:
            time.sleep(2)
    index = pc.Index(config.PINECONE_INDEX)
    emb = HuggingFaceEmbeddings(model_name=config.EMBED_MODEL)
    for ns, chunks in [(config.NS_CONTRACTS, contracts), (config.NS_POLICIES, policies)]:
        try:
            index.delete(delete_all=True, namespace=ns)     # re-running replaces, never duplicates
        except Exception:
            pass
        PineconeVectorStore(index=index, embedding=emb, namespace=ns, text_key="text").add_documents(
            chunks, ids=[c.metadata["chunk_id"] for c in chunks])
        print(f"  stored {len(chunks)} chunks in namespace '{ns}'")
    (config.ROOT / "index").mkdir(exist_ok=True)
    (config.ROOT / "index" / "chunks.json").write_text(json.dumps(
        [{"text": c.page_content, **c.metadata} for c in contracts + policies], indent=1))
    print("Done. Wait ~30 seconds before searching (Pinecone indexes in the background).")


if __name__ == "__main__":
    main()
