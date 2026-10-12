"""Environment configuration for the Runner Bot."""
import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

IDLE_TIMEOUT_SEC = int(os.environ.get("IDLE_TIMEOUT_SEC", "600"))
STATUS_REPORT_INTERVAL_SEC = int(os.environ.get("STATUS_REPORT_INTERVAL_SEC", "60"))
EXEC_TIMEOUT_SEC = int(os.environ.get("EXEC_TIMEOUT_SEC", "180"))
TG_BOT_POLLING = os.getenv("TG_BOT_POLLING", "1")  # '0' = 集中式模式（Oracle 消费，Runner 纯执行）
TG_SEND_FILE_LIMIT_MB = int(os.environ.get("TG_SEND_FILE_LIMIT_MB", "50"))
TG_RECV_FILE_LIMIT_MB = int(os.environ.get("TG_RECV_FILE_LIMIT_MB", "20"))

# oracle-agent client directory (for reading state.json + config.json)
AGENT_DIR = os.environ.get("AGENT_DIR", "/opt/oracle-agent")
