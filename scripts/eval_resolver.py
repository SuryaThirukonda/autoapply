"""Replay every question goat ever sent to Surya through the knowledge-base resolver, with the answer he
gave to that question hidden from it, and compare. Read-only: nothing is saved. Uses gpt-6-luna (a few
cents for the whole set).

    python scripts/eval_resolver.py [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main(limit: int) -> None:
    from goat import db
    from goat.agent.classifier import Classifier
    from goat.agent.knowledge import knowledge_text
    from goat.config import SETTINGS

    con = db.connect()
    rows = con.execute("SELECT p.id, p.key, p.question, p.options, p.field_type, p.answer, l.company, l.ats FROM pending_questions p "
                       "LEFT JOIN jobs j ON j.id=p.job_id LEFT JOIN listings l ON l.id=j.listing_id ORDER BY p.id").fetchall()[:limit]
    clf = Classifier()
    results = []
    for r in rows:
        kb = knowledge_text(r["company"] or "", r["ats"] or "")
        # hide what Surya answered to THIS question (its own key line), so the resolver must find it elsewhere
        kb = "\n".join(line for line in kb.splitlines() if not line.startswith(f"- {r['key']} |"))
        opts = json.loads(r["options"]) if r["options"] else []
        out = await clf.resolve(None, r["question"], opts, meaning=None, field_type=r["field_type"] or "", knowledge=kb, image_png=None) or {}
        ok_key = out.get("verdict") == "stored_key" and float(out.get("confidence") or 0) >= SETTINGS.models.resolver_min_confidence_key
        ok_der = out.get("verdict") == "derived" and float(out.get("confidence") or 0) >= SETTINGS.models.resolver_min_confidence_derived
        got = None
        if ok_key:
            a = con.execute("SELECT answer FROM answers WHERE key=? AND superseded_by IS NULL AND scope='global'", (out.get("key"),)).fetchone()
            got = a["answer"] if a else None
        elif ok_der:
            got = out.get("answer")
        results.append({"q": r["question"][:110], "options": opts[:6], "surya": r["answer"], "verdict": out.get("verdict"),
                        "key": out.get("key"), "would_answer": got, "confidence": out.get("confidence"),
                        "evidence": (out.get("evidence") or "")[:140]})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    settled = [x for x in results if x["would_answer"] is not None]
    print(f"\n{len(settled)} of {len(results)} would have been answered without asking Surya")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    asyncio.run(main(ap.parse_args().limit))
