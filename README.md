# DK AI Connector

Odoo 18 module that pairs an Odoo database with the [**DK AI Platform**](https://ai.dishonkadoh.com) server.

## Features

- Pairing with a one-time code (expires after 10 minutes)
- Connection status, connection test and revoke
- Read-only JSON API restricted to an allow-list of models
- Visible and usable by Odoo administrators only

## Installation

1. Copy `dk_ai_connector` into your addons path.
2. Install the Python dependency:

   ```bash
   pip install requests
   ```

3. Update the apps list and install **DK AI Connector**.

## Configuration

1. Open **DK AI > Configuration > DK AI Connection** (administrators only).
2. Set the DK AI Platform URL (use HTTPS).
3. Click **Generate Pairing Code** and confirm on the platform.

## API Endpoints

The connector exposes the following endpoints using a Bearer token issued during pairing:

- `POST /api/dk-ai/read`
- `POST /api/dk-ai/read-group`
- `POST /api/dk-ai/search-count`
- `POST /api/dk-ai/search-read`

Models outside the allow-list (`ALLOWED_MODELS` in `controllers/dk_ai_api.py`) are refused.

## Security Notes

- Only `base.group_system` has access to the connection model, menu and token.
- Use a dedicated Odoo user for the connection, with only the access it needs.
- Revoke the connection from the form to invalidate the token.

## License

LGPL-3. Copyright DK Business Solutions.
