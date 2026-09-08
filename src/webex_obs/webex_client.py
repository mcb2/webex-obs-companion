import logging
from pathlib import Path
import requests

logger = logging.getLogger(__name__)


class WebexClient:
    def __init__(
        self,
        token: str,
        recipient_email: str = "",
        room_id: str = "",
        my_agent_email: str = "",
    ):
        self.token = token
        self.recipient_email = recipient_email
        self.room_id = room_id
        self.my_agent_email = my_agent_email
        self.api_url = "https://webexapis.com/v1/messages"

    def send_transcript(self, transcript_path: Path, meeting_title: str = "Webex Meeting") -> bool:
        if not self.token:
            logger.error("Webex Bot access token is missing in .env. Cannot deliver transcript.")
            return False

        with open(transcript_path, encoding="utf-8") as f:
            transcript_text = f.read()

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

        # Build payload targeting either specific room_id or direct recipient email
        payload = {}
        target_desc = ""

        if self.room_id:
            payload["roomId"] = self.room_id
            target_desc = f"Space (roomId: {self.room_id})"
            prompt = (
                f"📋 **New Meeting Transcript ({meeting_title})**\n\n"
                "Here is the recorded and diarized meeting transcript:\n\n"
            )
        else:
            if not self.recipient_email:
                logger.error(
                    "WEBEX_RECIPIENT_EMAIL is missing in .env. "
                    "Cannot deliver a direct 1:1 message."
                )
                return False
            payload["toPersonEmail"] = self.recipient_email
            target_desc = f"1:1 Direct Message to {self.recipient_email}"
            agent_hint = f" ({self.my_agent_email})" if self.my_agent_email else ""
            prompt = (
                f"📋 **New Meeting Transcript ({meeting_title})**\n\n"
                "Here is your recorded and diarized meeting transcript. "
                f"You can forward this directly to My Agent{agent_hint} for an executive "
                "summary, decisions, and action items:\n\n"
            )

        try:
            if len(transcript_text) < 6000:
                payload["markdown"] = prompt + transcript_text
                res = requests.post(self.api_url, headers=headers, json=payload, timeout=30)
            else:
                payload["markdown"] = prompt + "*(Full transcript attached below due to Webex message length limits)*"
                files = {"files": (transcript_path.name, open(transcript_path, "rb"), "text/plain")}
                res = requests.post(self.api_url, headers=headers, data=payload, files=files, timeout=60)

            if res.status_code in (200, 201):
                logger.info(f"Successfully posted transcript to Webex: {target_desc}")
                return True
            else:
                logger.error(f"Webex API error ({res.status_code}): {res.text}")
                if res.status_code == 404:
                    if self.room_id:
                        logger.error(
                            f"Room ID '{self.room_id}' was not found. "
                            "Make sure your Bot has been added as a member of this Webex space. "
                            "Run 'uv run webex-obs list-rooms' to see valid room IDs."
                        )
                    else:
                        logger.error(
                            f"Recipient '{self.recipient_email}' could not be reached via 1:1 Bot message. "
                            "Note: Webex bots cannot 1:1 message other bots. Ensure "
                            "WEBEX_RECIPIENT_EMAIL in .env is set to your personal Webex email."
                        )
                return False
        except Exception as e:
            logger.error(f"Failed to post to Webex: {e}")
            return False

    def list_rooms(self) -> list[dict]:
        """List all spaces/rooms the bot is currently a member of."""
        if not self.token:
            return []
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        try:
            res = requests.get("https://webexapis.com/v1/rooms", headers=headers, timeout=15)
            if res.status_code == 200:
                return res.json().get("items", [])
            else:
                logger.error(f"Failed to list rooms ({res.status_code}): {res.text}")
                return []
        except Exception as e:
            logger.error(f"Failed to query Webex rooms: {e}")
            return []
