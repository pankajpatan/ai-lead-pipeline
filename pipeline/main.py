"""FastAPI app: enquiry intake, lead API and a demo page."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError

from .config import Settings
from .criteria import load_criteria
from .models import Enquiry
from .qualifier import Qualifier, ScriptedQualifier
from .service import Pipeline
from .store import STATES, Store

log = logging.getLogger("pipeline")
PAGE = Path(__file__).parent / "static" / "index.html"
basic = HTTPBasic(auto_error=False)


def create_app(settings: Settings | None = None, qualifier: Qualifier | None = None) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
    settings.validate()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    criteria = load_criteria(settings.criteria_path)
    if qualifier is None:
        qualifier = ScriptedQualifier()  # the Claude qualifier arrives in step 2
    store = Store(settings.database_path)
    pipeline = Pipeline(store, qualifier, criteria)

    app = FastAPI(title="AI Lead Pipeline", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pipeline = pipeline
    app.state.store = store

    def require_admin(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if settings.demo_mode:
            return
        ok = credentials is not None and secrets.compare_digest(
            credentials.password.encode(), settings.admin_password.encode()
        )
        if not ok:
            raise HTTPException(401, "Authentication required", {"WWW-Authenticate": "Basic"})

    def require_demo() -> None:
        if not settings.demo_mode:
            raise HTTPException(404, "Not found")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "qualifier": qualifier.name, "demo_mode": settings.demo_mode}

    @app.post("/intake", status_code=202)
    async def intake(request: Request, background: BackgroundTasks) -> JSONResponse:
        """Accepts JSON or a normal HTML form post."""
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                data = await request.json()
            except ValueError:
                raise HTTPException(400, "Body is not valid JSON")
        else:
            data = dict(await request.form())
        if not isinstance(data, dict):
            raise HTTPException(422, "Expected an object")
        try:
            enquiry = Enquiry.model_validate({k: v for k, v in data.items() if v not in ("", None)})
        except ValidationError as exc:
            errors = [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
            return JSONResponse({"ok": False, "errors": errors}, status_code=422)

        lead_id, outcome = pipeline.intake(enquiry)
        if outcome == "new":
            background.add_task(pipeline.qualify, lead_id)
        # A bot that filled the honeypot gets the same answer as a person, so it learns nothing.
        return JSONResponse(
            {"ok": True, "lead_id": lead_id, "duplicate": outcome == "duplicate"}, status_code=202
        )

    @app.get("/api/leads", dependencies=[Depends(require_admin)])
    def list_leads(state: str | None = None) -> list[dict]:
        if state and state not in STATES:
            raise HTTPException(422, f"state must be one of {sorted(STATES)}")
        return store.leads(state)

    @app.get("/api/leads/{lead_id}", dependencies=[Depends(require_admin)])
    def get_lead(lead_id: int) -> dict:
        lead = store.get(lead_id)
        if lead is None:
            raise HTTPException(404, "Lead not found")
        return {**lead, "events": store.events(lead_id)}

    @app.post("/api/leads/{lead_id}/retry", dependencies=[Depends(require_admin)])
    def retry(lead_id: int) -> dict:
        if not pipeline.retry(lead_id):
            raise HTTPException(409, "Only leads in needs_attention can be retried")
        return store.get(lead_id)

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_demo)])
    def demo_page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.post("/api/demo/reset", dependencies=[Depends(require_demo)])
    def reset() -> dict:
        store.reset()
        return {"ok": True}

    return app
