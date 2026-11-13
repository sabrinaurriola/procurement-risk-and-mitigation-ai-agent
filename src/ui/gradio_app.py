"""Local Gradio interface (thin view over src/ui/controller.py).

    python -m src.ui.gradio_app            # LIVE: real Tavily/GDELT/OpenAI; needs ALLOW_PAID_CALLS=1 (the approval is launching it that way)
    python -m src.ui.gradio_app --mock     # offline development mode: fixture research, still labelled MOCK

Binds 127.0.0.1 only, share=False, no tunnel, analytics off. This module deliberately reads no credential: the database URL, API keys and
the webhook secret/URLs are read server-side by the agent and the bridge client. Handlers return display text only.
Gradio is imported lazily so the rest of the code base (and its tests) never needs it."""
from __future__ import annotations

import argparse
import os
import sys

from src.ui.controller import COMMODITY_CHOICES, AppController, View, live_deps, mock_deps

HOST = "127.0.0.1"
PORT = 7860

# Light professional theme (Gradio 6: theme/css are launch() parameters). Forced light so the OS dark mode cannot change it.
THEME_JS = "() => { document.body.classList.remove('dark'); }"
CSS = """
.gradio-container { max-width: 1040px !important; margin: 0 auto; background: #ffffff !important; }
body { background: #ffffff !important; }
h1 { font-weight: 650; letter-spacing: -0.01em; }
.subtitle { color: #5b6878; margin-top: -8px; }
button.primary { background: #2f6fed !important; border: none !important; color: #ffffff !important; }
footer { display: none !important; }
"""


def build_ui(controller: AppController):
    import gradio as gr

    live = controller.mode == "live"
    with gr.Blocks(title="Procurement Risk Assessment", analytics_enabled=False) as demo:
        gr.Markdown("# Procurement Risk & Mitigation Agent\n<div class='subtitle'>Bakery Foods Inc. &middot; Decision support only: every report needs human review.</div>"
                    + ("" if live else "\n\n> **Development mode** - offline fixture research, not live market intelligence."))
        with gr.Row():
            ingredient = gr.Dropdown(COMMODITY_CHOICES, value="sunflower_oil", label="Commodity")
            horizon = gr.Slider(1, 6, value=3, step=1, label="Horizon (months)")
        run_btn = gr.Button("Run Assessment", variant="primary")
        message = gr.Markdown()
        status = gr.Textbox(label="Assessment status", interactive=False)
        warnings = gr.Markdown(label="Warnings")
        report = gr.Markdown(label="Report")
        archive_btn = gr.Button("Retry archive in Notion" if controller.auto_archive else "Archive in Notion", interactive=False)
        archive_msg = gr.Markdown()
        notion_link = gr.Textbox(label="Notion page (confirmed)", interactive=False)
        confirm = gr.Checkbox(label="The previous e-mail result is unknown and I checked the inbox: allow ONE manual resend", value=False, visible=False)
        email_btn = gr.Button("Send also by email", interactive=False)
        email_msg = gr.Markdown()

        outputs = [message, status, warnings, report, archive_msg, notion_link, email_msg, run_btn, archive_btn, email_btn, confirm]

        def render(v: View) -> tuple:
            return (v.message, v.assessment_status, v.warnings, v.report_markdown, v.archive_message, v.notion_url, v.email_message,
                    gr.update(interactive=v.run_enabled), gr.update(interactive=v.archive_enabled), gr.update(interactive=v.email_enabled),
                    gr.update(visible=v.confirm_resend_visible, value=False))

        def on_run(ing, hz):
            yield render(controller.busy_view("Running the assessment" + (" and archiving it in Notion" if controller.auto_archive else "")
                                              + ". Research and drafting can take 1-3 minutes; do not close this page."))
            yield render(controller.run(ing, int(hz), None, True))

        def on_archive():
            yield render(controller.busy_view("Retrying the Notion archive via n8n..."))
            yield render(controller.archive())

        def on_email(ok):
            yield render(controller.busy_view("Sending the e-mail via n8n (one attempt)..."))
            yield render(controller.email(bool(ok)))

        run_btn.click(on_run, [ingredient, horizon], outputs, concurrency_limit=1, api_visibility="private")
        archive_btn.click(on_archive, None, outputs, concurrency_limit=1, api_visibility="private")
        email_btn.click(on_email, [confirm], outputs, concurrency_limit=1, api_visibility="private")
    return demo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="offline demo (fixture research); default is LIVE")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--max-live-runs", type=int, default=1, help="paid assessments allowed in this session (default 1)")
    args = ap.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import gradio as gr
    controller = AppController(mock_deps if args.mock else live_deps, mode="mock" if args.mock else "live", max_live_runs=args.max_live_runs)
    demo = build_ui(controller)
    demo.queue(default_concurrency_limit=1)
    demo.launch(server_name=HOST, server_port=args.port, share=False, show_error=False, inbrowser=False,
                theme=gr.themes.Base(primary_hue="blue", neutral_hue="slate"), css=CSS, js=THEME_JS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
