#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# This fork is intentionally restricted to Always Free E2.1.Micro instances.

read -p "Micro instance name prefix [micro]: " MICRO_NAME_PREFIX
MICRO_NAME_PREFIX="${MICRO_NAME_PREFIX:-micro}"
clear

read -p "Enter the regional Subnet OCID: " SUBNET_ID
clear

read -p "Enter the Image OCID (or press Enter to skip): " IMAGE_ID
clear

while true; do
    read -p "Enable Gmail notification? (y/n): " BOOL_MAIL
    
    BOOL_MAIL=$(echo "$BOOL_MAIL" | tr '[:upper:]' '[:lower:]')

    case $BOOL_MAIL in
        y)
            BOOL_MAIL="True"
            break
            ;;
        n)
            BOOL_MAIL="False"
            break
            ;;
        *)
            clear
            echo "Invalid choice. Please try again (CTRL+C to quit)"
            ;;
    esac
done
clear

if [[ $BOOL_MAIL == "True" ]]; then
    read -p "Enter your email: " EMAIL
    clear

    read -p "Enter email app password (16 characters without spaces): " EMAIL_PASS
    clear
fi

read -p "Enter Discord webhook URL (or press Enter to skip): " DISCORD_WEBHOOK
clear

read -p "Enter Telegram bot token (or press Enter to skip): " TELEGRAM_TOKEN
clear

read -p "Enter Telegram user ID (or press Enter to skip): " TELEGRAM_USER_ID
clear

# Backup existing oci.env if it exists
if [ -f oci.env ]; then
    mv oci.env oci.env.bak
    echo "Existing oci.env file backed up as oci.env.bak"
fi

# Create the new oci.env file with the gathered configuration
cat <<EOF > oci.env
# OCI Configuration
OCI_CONFIG=$REPO_DIR/oci_config
MICRO_AD=AD-1
# Fixed by code; this fork refuses any other shape.
OCI_COMPUTE_SHAPE=VM.Standard.E2.1.Micro
REQUEST_WAIT_TIME_SECS=120
MICRO_SSH_PUBLIC_KEY=$REPO_DIR/id_rsa.pub
# Regional subnet used only for VM.Standard.E2.1.Micro launches.
OCI_SUBNET_ID="$SUBNET_ID"
OCI_IMAGE_ID="$IMAGE_ID"
# The following will be ignored if OCI_IMAGE_ID is specified
OPERATING_SYSTEM="Canonical Ubuntu"
OS_VERSION=22.04
BOOT_VOLUME_SIZE=50

# Gmail Notification
NOTIFY_EMAIL=$BOOL_MAIL
EMAIL="$EMAIL"
EMAIL_PASSWORD="$EMAIL_PASS"

# Discord Notification (optional)
DISCORD_WEBHOOK="$DISCORD_WEBHOOK"

# Telegram Notification (optional)
TELEGRAM_TOKEN="$TELEGRAM_TOKEN"
TELEGRAM_USER_ID="$TELEGRAM_USER_ID"

# Safe sequential Micro hunter settings
MICRO_NAME_PREFIX="$MICRO_NAME_PREFIX"
MAX_RUNTIME_HOURS=6
MAX_LAUNCH_ATTEMPTS=180
MAX_BACKOFF_SECS=900
MAX_RATE_LIMIT_FAILURES=5
RECONCILE_GRACE_SECS=180
MAX_TOTAL_BLOCK_VOLUME_GB=200
EOF

chmod 600 oci.env
echo "OCI env configuration saved to oci.env"
