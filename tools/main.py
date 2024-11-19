import os
import requests
import argparse
import tempfile
from urllib.parse import urlparse, parse_qs
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from litellm import completion
import base64
from typing import Dict, List, Tuple, Optional
import logging
import json

# Constants
DEFAULT_ORG_ID = "1"
IMAGE_WIDTH = 1000
IMAGE_HEIGHT = 500
TIME_RANGE = "1h"

# Add at the top of the file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def generate_grafana_api_url(grafana_dashboard_url: str) -> Tuple[str, str]:
    parsed_url = urlparse(grafana_dashboard_url)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) >= 3 and path_parts[0] == "d":
        dashboard_uid = path_parts[1]
    else:
        raise ValueError("URL path does not have the expected format /d/{uid}/{slug}")

    query_params = parse_qs(parsed_url.query)
    org_id = query_params.get("orgId", [DEFAULT_ORG_ID])[0]

    api_url = f"{parsed_url.scheme}://{parsed_url.netloc}/api/dashboards/uid/{dashboard_uid}"
    return api_url, org_id

def get_dashboard_panels(api_url, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()  # Raises an HTTPError for bad responses
        dashboard_data = response.json()
        panels = dashboard_data.get('dashboard', {}).get('panels', [])
        if not panels:
            raise ValueError("No panels found in dashboard")
        return [(panel.get('title'), panel.get('id')) for panel in panels if 'title' in panel and 'id' in panel]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch dashboard data: {str(e)}")
        raise

def generate_grafana_render_url(grafana_dashboard_url, panel_id):
    parsed_url = urlparse(grafana_dashboard_url)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) >= 3 and path_parts[0] == "d":
        dashboard_uid = path_parts[1]
        dashboard_slug = path_parts[2]
    else:
        raise ValueError("URL path does not have the expected format /d/{uid}/{slug}")

    query_params = parse_qs(parsed_url.query)
    org_id = query_params.get("orgId", [DEFAULT_ORG_ID])[0]

    render_url = f"{parsed_url.scheme}://{parsed_url.netloc}/render/d-solo/{dashboard_uid}/{dashboard_slug}?orgId={org_id}&from=now-{TIME_RANGE}&to=now&panelId={panel_id}&width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}"
    return render_url, org_id

def download_panel_image(render_url: str, api_key: str, panel_title: str) -> Optional[bytes]:
    try:
        response = requests.get(render_url, headers={"Authorization": f"Bearer {api_key}"}, stream=True)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download panel image {panel_title}: {str(e)}")
        return None

def send_slack_file_to_thread(token, channel_id, thread_ts, file_path, initial_comment):
    client = WebClient(token=token)
    try:
        response = client.files_upload_v2(
            channel=channel_id,
            file=file_path,
            initial_comment=initial_comment,
            thread_ts=thread_ts
        )
        return response
    except SlackApiError as e:
        logger.error(f"Failed to upload file to Slack: {str(e)}")
        raise

def extract_slack_response_info(response):
    return {
        "ok": response.get("ok"),
        "file_id": response.get("file", {}).get("id"),
        "file_name": response.get("file", {}).get("name"),
        "file_url": response.get("file", {}).get("url_private"),
        "timestamp": response.get("file", {}).get("timestamp")
    }

def analyze_image_with_vision_model(
    image_content: bytes,
    panel_title: str,
    alert_data: Dict
) -> Optional[Dict[str, str]]:
    llm_key = os.environ["VISION_LLM_KEY"]
    llm_base_url = os.environ["VISION_LLM_BASE_URL"]

    base64_image = base64.b64encode(image_content).decode('utf-8')

    prompt = f"""You are analyzing a Grafana panel titled '{panel_title}' in relation to an alert.

Alert Context:
{json.dumps(alert_data, indent=2)}

Looking at the Grafana panel image:
1. Is this panel relevant to understanding or diagnosing the alert condition? Consider metrics, service names, and any other contextual relationships.
2. If relevant, does the panel data indicate any anomalies or unusual patterns that could be related to the alert?
3. Based on the panel data, what specific actions should be taken to address the issue?

Start your response with either:
"Anomaly detected:" followed by your analysis if the panel shows an anomaly related to the alert
OR
"No anomaly" if the panel doesn't show any unusual patterns related to the alert condition."""

    try:
        response = completion(
            model="openai/gpt-4o",
            api_key=llm_key,
            base_url=llm_base_url,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            },
                        },
                    ],
                }
            ],
        )
        analysis = response.choices[0].message.content
        if analysis.lower().startswith('anomaly detected'):
            return {
                "analysis": analysis,
                "panel_title": panel_title
            }
        return None
    except Exception as e:
        logger.error(f"Error analyzing image: {str(e)}")
        return None

def find_related_panels(panels: List[Tuple[str, int]], alert_info: Dict, grafana_dashboard_url: str, grafana_api_key: str) -> List[Dict]:
    related_panels = []
    for panel_title, panel_id in panels:
        logger.info(f"Analyzing panel: {panel_title}")
        render_url, org_id = generate_grafana_render_url(grafana_dashboard_url, panel_id)
        image_content = download_panel_image(render_url, grafana_api_key, panel_title)
        
        if image_content:
            analysis_result = analyze_image_with_vision_model(image_content, panel_title, alert_info)
            if analysis_result:
                panel_info = {
                    "title": panel_title,
                    "image_content": image_content,
                    "analysis": analysis_result["analysis"],
                    "render_url": render_url,
                    "org_id": org_id
                }
                related_panels.append(panel_info)
        else:
            logger.warning(f"Failed to download image for panel: {panel_title}")

    return related_panels

def find_grafana_url(obj) -> Optional[str]:
    """Recursively search through a dictionary/list to find a Grafana dashboard URL"""
    if isinstance(obj, str) and '/d/' in obj:
        parsed = urlparse(obj)
        if parsed.scheme and parsed.netloc:
            return obj
    elif isinstance(obj, dict):
        for value in obj.values():
            found_url = find_grafana_url(value)
            if found_url:
                return found_url
    elif isinstance(obj, list):
        for item in obj:
            found_url = find_grafana_url(item)
            if found_url:
                return found_url
    return None

def send_panel_to_slack(
    panel_info: Dict,
    slack_token: str,
    channel_id: str,
    thread_ts: str,
    grafana_dashboard_url: str
) -> Dict:
    """
    Send a single panel's information and image to Slack
    Returns the Slack response info
    """
    initial_comment = (
        f"📊 *Grafana Panel: {panel_info['title']}*\n"
        f"Dashboard: {grafana_dashboard_url}\n"
        f"Render URL: {panel_info['render_url']}\n"
        f"Org ID: {panel_info['org_id']}\n\n"
        f"*Analysis:*\n{panel_info['analysis']}"
    )
    
    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_file:
        temp_file.write(panel_info['image_content'])
        temp_file_path = temp_file.name
        
    try:
        slack_response = send_slack_file_to_thread(
            slack_token,
            channel_id,
            thread_ts,
            temp_file_path,
            initial_comment
        )
        return extract_slack_response_info(slack_response)
    finally:
        os.remove(temp_file_path)

def main():
    required_env_vars = [
        "SLACK_THREAD_TS",
        "SLACK_CHANNEL_ID",
        "SLACK_API_TOKEN",
        "GRAFANA_API_KEY",
        "VISION_LLM_KEY",
        "VISION_LLM_BASE_URL"
    ]
    
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
        raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
    
    parser = argparse.ArgumentParser(description="Process Grafana dashboard and alert data.")
    parser.add_argument("--alert_payload", required=True, help="JSON string containing the alert payload")
    args = parser.parse_args()

    try:
        alert_data = json.loads(args.alert_payload)
        grafana_dashboard_url = find_grafana_url(alert_data)
        if not grafana_dashboard_url:
            raise ValueError("Could not find Grafana dashboard URL in alert payload")
        
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON payload")
    except Exception as e:
        raise ValueError(f"Error processing alert payload: {str(e)}")

    thread_ts = os.environ.get("SLACK_THREAD_TS")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    slack_token = os.environ.get("SLACK_API_TOKEN")
    grafana_api_key = os.environ.get("GRAFANA_API_KEY")

    api_url, org_id = generate_grafana_api_url(grafana_dashboard_url)
    all_panels = get_dashboard_panels(api_url, grafana_api_key)

    # Pass the entire alert_data to find_related_panels for context
    related_panels = find_related_panels(all_panels, alert_data, grafana_dashboard_url, grafana_api_key)

    if not related_panels:
        logger.info("No relevant panels found")
        return

    # Send initial summary message
    summary_message = (
        f"🔍 *Analysis Summary*\n"
        f"Found {len(related_panels)} relevant panels with anomalies in the dashboard.\n"
        f"Each panel will be posted below with its analysis."
    )
    
    client = WebClient(token=slack_token)
    try:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=summary_message
        )
    except SlackApiError as e:
        logger.error(f"Failed to send summary message: {str(e)}")

    # Send each panel's information and image
    for panel_info in related_panels:
        try:
            response_info = send_panel_to_slack(
                panel_info,
                slack_token,
                channel_id,
                thread_ts,
                grafana_dashboard_url
            )
            logger.info(f"Successfully sent panel {panel_info['title']}: {response_info}")
        except Exception as e:
            logger.error(f"Failed to send panel {panel_info['title']}: {str(e)}")

if __name__ == "__main__":
    main()
