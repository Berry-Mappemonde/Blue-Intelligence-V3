"""app.state — Singletons shared across routers (discovery swarm)."""
from app.db import db
from app.services.swarm_pipeline import Swarm

swarm = Swarm(db)
