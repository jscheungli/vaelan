"""Planificateur minimal (thread dans le process web) : tâches quotidiennes à heure fixe (heure de La
Réunion, UTC+4), sans double exécution après redémarrage (dernière date mémorisée dans Setting)."""
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, List, Tuple

from sqlmodel import Session, select

from app.core.db import engine
from app.models import Setting

_TZ = timedelta(hours=4)
_TASKS: List[Tuple[str, int, Callable[[], None]]] = []     # (nom, heure locale, fonction)


def register(name: str, hour_local: int, fn: Callable[[], None]) -> None:
    _TASKS.append((name, hour_local, fn))


def _last(name: str) -> str:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == "_system", Setting.key == f"sched:{name}")).first()
        return st.value if st else ""


def _mark(name: str, day: str) -> None:
    with Session(engine) as s:
        st = s.exec(select(Setting).where(Setting.company_code == "_system", Setting.key == f"sched:{name}")).first()
        if not st:
            st = Setting(company_code="_system", key=f"sched:{name}", value=day)
        st.value = day
        st.updated_at = datetime.utcnow()
        s.add(st)
        s.commit()


def _loop():
    time.sleep(90)                       # laisser l'application démarrer
    while True:
        try:
            now = datetime.utcnow() + _TZ
            today = now.strftime("%Y-%m-%d")
            for name, hour, fn in _TASKS:
                if now.hour >= hour and _last(name) != today:
                    _mark(name, today)
                    try:
                        fn()
                    except Exception as e:
                        print(f"[scheduler] {name} : {e}")
        except Exception as e:
            print(f"[scheduler] boucle : {e}")
        time.sleep(300)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="vaelan-scheduler").start()
