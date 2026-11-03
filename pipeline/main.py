"""FastAPI app: enquiry intake, lead API and a demo page."""
from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, ValidationError

from .alerts import LogNotifier, Notifier, SlackNotifier
from .config import Settings
from .crm import CRM, FakeCRM, HubSpotCRM
from .criteria import load_criteria
from .drafts import Drafter, TemplateDrafter
from .mailer import Mailer, OutboxMailer, SMTPMailer
from .models import Enquiry
from .qualifier import Qualifier, ScriptedQualifier
from .service import Pipeline
from .store import STATES, InvalidTransition, Store

log = logging.getLogger("pipeline")
STATIC = Path(__file__).parent / "static"
basic = HTTPBasic(auto_error=False)


def require_same_origin(request: Request) -> None:
    """Block cross-site requests to actions (CSRF): browsers resend basic-auth credentials
    automatically, so a page on another site could otherwise approve or send for you."""
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-site request blocked")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-site request blocked")


class Approval(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    confirm_warnings: bool = False


class Rejection(BaseModel):
    reason: str = Field(default="", max_length=300)


def create_app(settings: Settings | None = None, qualifier: Qualifier | None = None,
               crm: CRM | None = None, drafter: Drafter | None = None,
               mailer: Mailer | None = None, notifier: Notifier | None = None) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
    settings.validate()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    criteria = load_criteria(settings.criteria_path)
    if qualifier is None:
        if settings.anthropic_api_key:
            from .claude import ClaudeQualifier
            qualifier = ClaudeQualifier(settings.anthropic_api_key, settings.model, settings.effort)
        else:
            qualifier = ScriptedQualifier()
    if crm is None:
        crm = HubSpotCRM(settings.hubspot_token) if settings.hubspot_token else FakeCRM()
    if drafter is None:
        if settings.anthropic_api_key:
            from .drafts import ClaudeDrafter
            drafter = ClaudeDrafter(settings.anthropic_api_key, settings.model, settings.effort)
        else:
            drafter = TemplateDrafter()
    if mailer is None:
        if settings.smtp_host:
            mailer = SMTPMailer(settings.smtp_host, settings.smtp_port, settings.mail_from, settings.smtp_user,
                                settings.smtp_password, settings.smtp_starttls, settings.reply_to or None)
        else:
            mailer = OutboxMailer(settings.mail_from or "demo@localhost")
    if notifier is None:
        notifier = SlackNotifier(settings.alert_webhook_url) if settings.alert_webhook_url else LogNotifier()
    store = Store(settings.database_path)
    pipeline = Pipeline(store, qualifier, criteria, crm, drafter, mailer, notifier, settings.public_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Finish leads left mid-pipeline by a restart, without delaying startup.
        threading.Thread(target=pipeline.resume_pending, name="resume-pending", daemon=True).start()
        yield

    app = FastAPI(title="AI Lead Pipeline", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.store = store
    app.state.mailer = mailer
    app.state.notifier = notifier

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
        return {"ok": True, "qualifier": qualifier.name, "crm": crm.name, "drafter": drafter.name,
                "mailer": mailer.name, "alerts": notifier.name, "demo_mode": settings.demo_mode}

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
            background.add_task(pipeline.process, lead_id)
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

    actions = [Depends(require_admin), Depends(require_same_origin)]

    @app.post("/api/leads/{lead_id}/retry", dependencies=actions)
    def retry(lead_id: int) -> dict:
        if not pipeline.retry(lead_id):
            raise HTTPException(409, "Only leads in needs_attention can be retried")
        return store.get(lead_id)

    @app.post("/api/leads/{lead_id}/approve", dependencies=actions)
    def approve(lead_id: int, approval: Approval, background: BackgroundTasks) -> JSONResponse:
        """Approve the draft (edited or not). Sending runs right after, in the background."""
        try:
            result = pipeline.approve(lead_id, approval.subject, approval.body, approval.confirm_warnings)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc))
        if not result["ok"]:
            return JSONResponse({**result, "message": "Confirm the warnings to approve anyway."}, status_code=409)
        background.add_task(pipeline.process, lead_id)
        return JSONResponse(result)

    @app.post("/api/leads/{lead_id}/reject", dependencies=actions)
    def reject(lead_id: int, rejection: Rejection) -> dict:
        try:
            pipeline.reject(lead_id, rejection.reason)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc))
        return store.get(lead_id)

    @app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    def dashboard() -> HTMLResponse:
        return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"),
                            headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                     "style-src 'unsafe-inline'; frame-ancestors 'none'"})

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_demo)])
    def demo_page() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.post("/api/demo/reset", dependencies=[Depends(require_demo), Depends(require_same_origin)])
    def reset() -> dict:
        store.reset()
        return {"ok": True}

    return app
