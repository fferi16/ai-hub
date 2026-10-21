"""Párbeszéd-próba mikrofon és Claude nélkül: az első válasz kérdés → a gép magától meghallgatja a választ
(itt a „mikrofon" gépi hangon mondja), a második válasz nem kérdés → vége.   python plugins\\voice\\tests\\dialog_check.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[2]), str(HERE.parent)]
import dictate  # noqa: E402

dictate.big_model(quiet=True)          # a GPU-s modell ELŐBB, mint a WinRT-s hangkészítés
import assistant  # noqa: E402
import engine  # noqa: E402
import wake_train as wt  # noqa: E402

reply = wt._synth("Az Edge-et.", "hu", 1.0, 1.0) * 0.8
asked, recorded = [], []


def fake_ask(command, backend, on_sentence=None):
    asked.append(command)
    ans = "Melyik böngészőt nyissam meg?" if len(asked) == 1 else "Rendben, megnyitottam az Edge-et."
    return {"answer": ans, "backend": "teszt", "seconds": 0.1}


def fake_record(**kw):
    recorded.append(kw)
    return (reply if len(recorded) == 1 else None), {}


assistant.ask = fake_ask
engine.record = fake_record
dictate.speak = lambda s: None
dictate.show = lambda *a, **k: None
dictate.log = lambda m: print("  napló:", m)
dictate.answer("Nyisd meg a böngészőt.", "claude")
ok = len(asked) == 2 and "edge" in asked[1].lower() and len(recorded) == 1
print(f"kérések: {asked}; meghallgatások: {len(recorded)} → {'OK' if ok else 'HIBA'}")
sys.exit(0 if ok else 1)
