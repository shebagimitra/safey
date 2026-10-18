# ============================================================
# COMPONENT: C11a / Cloud Entrypoint - Hugging Face Space App
# PURPOSE: Space entrypoint serving Gradio UI and mounted FastAPI endpoints on port 7860.
# INPUT: HTTP requests from Hugging Face Space proxy.
# OUTPUT: Full aviation safety investigation dashboard and REST API.
# CALLED BY: Hugging Face Space runner      CALLS: api.main
# IF IT BREAKS, CHECK: PORT environment variable, database initialization, static file mounts.
# ============================================================

import os
from api.main import app as fastapi_app, init_db_if_missing

# Ensure replay-only mode and default paths in cloud deployment
os.environ.setdefault("ALLOW_LIVE", "false")
os.environ.setdefault("DATA_DIR", "data")

init_db_if_missing()

try:
    import gradio as gr
    import spaces

    @spaces.GPU
    def check_system():
        return "Operational"

    with gr.Blocks(title="Aviation Safety Evidence Pipeline", css="footer {visibility: hidden}") as demo:
        gr.Markdown("### Aviation Safety Investigation System — Replay & Verification Pipeline")
        gr.HTML(
            '<div style="width:100%; height:92vh; margin:0; padding:0;">'
            '<iframe src="/frontend/index.html" style="width:100%; height:100%; border:none;"></iframe>'
            '</div>'
        )
        dummy_btn = gr.Button("Status", visible=False)
        dummy_btn.click(fn=check_system, outputs=[])

    # Mount FastAPI routes and handlers onto Gradio's internal FastAPI app
    for route in fastapi_app.routes:
        demo.app.routes.append(route)

    if __name__ == "__main__":
        demo.launch(server_name="0.0.0.0", server_port=7860)

except Exception:
    if __name__ == "__main__":
        import uvicorn
        port = int(os.environ.get("PORT", 7860))
        uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
