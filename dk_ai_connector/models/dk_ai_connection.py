import secrets
import requests

from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class DkAiConnection(models.Model):
    _name = "dk.ai.connection"
    _description = "DK AI Connection"
    _rec_name = "name"

    name = fields.Char(
        string="Name",
        required=True,
        default="DK AI Connection",
    )

    user_id = fields.Many2one(
        "res.users",
        string="Odoo User",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
        copy=False,
    )

    dk_ai_url = fields.Char(
        string="DK AI URL",
        required=True,
        default="http://127.0.0.1:8080",
    )

    status = fields.Selection(
        [
            ("disconnected", "Disconnected"),
            ("pending", "Pending"),
            ("connected", "Connected"),
            ("error", "Error"),
        ],
        string="Status",
        default="disconnected",
        required=True,
    )

    pairing_code = fields.Char(
        string="Pairing Code",
        readonly=True,
        copy=False,
    )

    pairing_expires_at = fields.Datetime(
        string="Pairing Expires At",
        readonly=True,
        copy=False,
    )

    connection_id = fields.Char(
        string="Connection ID",
        readonly=True,
        copy=False,
    )

    connector_token = fields.Char(
        string="Connector Token",
        readonly=True,
        copy=False,
        groups="base.group_system",
    )

    connector_token_created_at = fields.Datetime(
        string="Token Created At",
        readonly=True,
        copy=False,
    )

    connected_at = fields.Datetime(
        string="Connected At",
        readonly=True,
        copy=False,
    )

    last_test_at = fields.Datetime(
        string="Last Connection Test",
        readonly=True,
    )

    active = fields.Boolean(
        default=True,
    )

    def _generate_pairing_code(self):
        """
        Generate a high-entropy human-readable pairing code.
        """

        first = secrets.token_hex(4).upper()
        second = secrets.token_hex(4).upper()

        return f"DKAI-{first}-{second}"

    def action_generate_pairing_code(self):
        self.ensure_one()

        if not self.dk_ai_url:
            raise UserError(
                "Please configure the DK AI URL first."
            )

        pairing_code = self._generate_pairing_code()

        expires_at = fields.Datetime.now() + timedelta(minutes=10)

        # A stable identifier for this Odoo installation.
        instance_uid = self._get_instance_uid()

        if not instance_uid:
            instance_uid = self.env.cr.dbname

        try:
            response = requests.post(
                f"{self.dk_ai_url.rstrip('/')}/api/odoo/pairing/register",
                json={
                    "pairing_code": pairing_code,
                    "odoo_url": self.env["ir.config_parameter"]
                    .sudo()
                    .get_str("web.base.url", default=""),
                    "database": self.env.cr.dbname,
                    "instance_uid": instance_uid,
                },
                timeout=15,
            )

            if response.status_code >= 400:
                raise UserError(
                    "DK AI rejected the pairing request:\n\n"
                    + response.text
                )

        except requests.RequestException as error:
            raise UserError(
                "Could not reach DK AI Platform:\n\n"
                + str(error)
            )

        self.write({
            "pairing_code": pairing_code,
            "pairing_expires_at": expires_at,
            "status": "pending",
        })

        return True

    def _exchange_pairing_credentials(self):
        """Exchange the pending pairing code and persist its credential."""
        self.ensure_one()

        if not self.dk_ai_url:
            raise UserError(
                "Please configure the DK AI URL first."
            )

        if not self.pairing_code:
            raise UserError(
                "No pairing code is available. Generate a pairing code first."
            )

        if not self.pairing_expires_at:
            raise UserError(
                "The pairing code has no expiry time."
            )

        if self.pairing_expires_at < fields.Datetime.now():
            raise UserError(
                "The pairing code has expired. Generate a new pairing code."
            )

        instance_uid = self._get_instance_uid()

        try:
            response = requests.post(
                f"{self.dk_ai_url.rstrip('/')}/api/odoo/pairing/exchange",
                json={
                    "pairing_code": self.pairing_code,
                    "instance_uid": instance_uid,
                },
                timeout=15,
            )

            if response.status_code >= 400:
                raise UserError(
                    "DK AI rejected the credential exchange."
                )

            data = response.json()

        except requests.RequestException as error:
            raise UserError(
                "Could not reach DK AI Platform:\n\n" + str(error)
            )

        except ValueError:
            raise UserError(
                "DK AI returned an invalid response during credential exchange."
            )

        if not data.get("exchanged"):
            raise UserError(
                "DK AI did not complete the credential exchange."
            )

        connector_token = data.get("connector_token")
        if not connector_token:
            raise UserError(
                "DK AI did not return a connector credential."
            )

        self.write({
            "connection_id": str(data.get("connection_id")),
            "connector_token": connector_token,
            "connector_token_created_at": fields.Datetime.now(),
            "connected_at": fields.Datetime.now(),
            "status": "connected",
            "pairing_code": False,
            "pairing_expires_at": False,
        })

        return True

    def action_exchange_pairing(self):
        """Keep manual exchange available as a fallback."""
        return self._exchange_pairing_credentials()

    def action_test_connection(self):
        self.ensure_one()

        if not self.dk_ai_url:
            raise UserError(
                "Please configure the DK AI URL first."
            )

        try:
            response = requests.get(
                f"{self.dk_ai_url.rstrip('/')}/health",
                timeout=10,
            )

            response.raise_for_status()

        except requests.RequestException as error:
            self.write({
                "status": "error",
                "last_test_at": fields.Datetime.now(),
            })

            raise UserError(
                "DK AI connection failed:\n\n"
                + str(error)
            )

        self.write({
            "last_test_at": fields.Datetime.now(),
        })

        return True

    def action_revoke_connection(self):
        self.ensure_one()

        if self.connector_token and self.dk_ai_url:
            try:
                response = requests.post(
                    f"{self.dk_ai_url.rstrip('/')}/api/odoo/pairing/revoke",
                    headers={
                        "Authorization": f"Bearer {self.connector_token}",
                    },
                    timeout=15,
                )

                if response.status_code >= 400:
                    raise UserError(
                        "DK AI rejected the credential revocation:\n\n"
                        + response.text
                    )

            except requests.RequestException as error:
                raise UserError(
                    "Could not reach DK AI Platform to revoke the credential:\n\n"
                    + str(error)
                )

        self.write({
            "status": "disconnected",
            "pairing_code": False,
            "pairing_expires_at": False,
            "connection_id": False,
            "connector_token": False,
            "connector_token_created_at": False,
            "connected_at": False,
        })

        return True

    @api.model
    def get_connection(self):
        return self.search(
            [
                ("active", "=", True),
            ],
            limit=1,
        )

    def _get_instance_uid(self):
        ICP = self.env["ir.config_parameter"].sudo()

        instance_uid = ICP.get_str(
            "dk_ai_connector.instance_uid",
            default="",
        )

        if not instance_uid:
            instance_uid = secrets.token_hex(16)
            ICP.set_str(
                "dk_ai_connector.instance_uid",
                instance_uid,
            )

        return instance_uid

