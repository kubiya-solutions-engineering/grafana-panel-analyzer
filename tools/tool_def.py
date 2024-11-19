from . import main

import inspect

from kubiya_sdk import tool_registry
from kubiya_sdk.tools.models import Arg, Tool, FileSpec

analyze_grafana_panel = Tool(
    name="analyze_grafana_panel",
    description="Generate render URLs for relevant Grafana dashboard panels, download images, analyze them using OpenAI's vision model, and send results to the current Slack thread",
    type="docker",
    image="python:3.12",
    content="""
pip install slack_sdk > /dev/null 2>&1
pip install requests > /dev/null 2>&1
pip install litellm==1.49.5 > /dev/null 2>&1
pip install pillow==11.0.0 > /dev/null 2>&1

python /tmp/grafana.py \
    --alert_payload "$alert_payload"
""",
    secrets=[
        "SLACK_API_TOKEN", 
        "GRAFANA_API_KEY", 
        "VISION_LLM_KEY"
    ],
    env=[
        "SLACK_THREAD_TS", 
        "SLACK_CHANNEL_ID",
        "VISION_LLM_BASE_URL"
    ],
    args=[
        Arg(name="alert_payload", description="JSON string containing the alert payload", required=True)
    ],
    with_files=[
        FileSpec(
            destination="/tmp/grafana.py",
            content=inspect.getsource(main),
        )
    ]
)

# Register the updated tool
tool_registry.register("grafana", analyze_grafana_panel)