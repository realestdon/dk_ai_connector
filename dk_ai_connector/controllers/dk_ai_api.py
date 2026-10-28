import hmac
import json
import logging

from odoo import fields, http
from odoo.api import Environment
from odoo.http import request
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Models the connector may read. Everything else is refused.
ALLOWED_MODELS = {
    "ir.module.module",
    "account.account",
    "account.account.tag",
    "account.journal",
    "account.move",
    "account.move.line",
    "account.tax",
    "crm.lead",
    "product.product",
    "product.template",
    "purchase.order",
    "purchase.order.line",
    "res.company",
    "res.currency",
    "res.partner",
    "res.users",
    "sale.order",
    "sale.order.line",
    "stock.location",
    "stock.move",
    "stock.quant",
    "stock.valuation.layer",
}

# search_read additionally exposes activities.
SEARCH_READ_MODELS = ALLOWED_MODELS | {"mail.activity", "account.partial.reconcile"}


class DkAiApiController(http.Controller):

    def _get_connector(self):
        authorization = request.httprequest.headers.get("Authorization")

        if not authorization:
            return None

        scheme, _, token = authorization.partition(" ")

        if scheme.lower() != "bearer" or not token:
            return None

        connection = request.env["dk.ai.connection"].sudo().search(
            [
                ("active", "=", True),
                ("connector_token", "=", token),
            ],
            limit=1,
        )

        return connection or None

    def _get_connector_env(self, company_ids=None):
        connection = self._get_connector()

        if not connection:
            return None, None

        connector_user = connection.user_id

        if not connector_user:
            return None, None

        available_company_ids = connector_user.company_ids.ids

        if company_ids:
            try:
                requested_company_ids = [
                    int(company_id)
                    for company_id in company_ids
                ]
            except (TypeError, ValueError):
                return None, None

            # Reject the entire request if any company is inaccessible.
            # Do not silently broaden or change the requested scope.
            if (
                not requested_company_ids
                or len(requested_company_ids) != len(set(requested_company_ids))
                or any(
                    company_id not in available_company_ids
                    for company_id in requested_company_ids
                )
            ):
                return None, None

            company_ids = requested_company_ids
        else:
            # An empty scope means all companies assigned to this
            # connector user, never every company in the database.
            company_ids = available_company_ids

        if not company_ids:
            return None, None

        connector_context = {
            key: value
            for key, value in request.env.context.items()
            if key != "allowed_company_ids"
        }
        connector_context["allowed_company_ids"] = company_ids

        env = Environment(
            request.env.cr,
            connector_user.id,
            connector_context,
            su=True,
        )

        return connection, env

    @http.route(
        "/api/dk-ai/read",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def read(self, model=None, ids=None, fields=None, company_ids=None):
        connection, env = self._get_connector_env(
            company_ids=company_ids,
        )

        if not connection:
            return {
                "error": "Unauthorized connector",
            }

        if not model:
            return {
                "error": "Model is required",
            }

        allowed_models = ALLOWED_MODELS

        if model not in allowed_models:
            return {
                "error": "Model is not allowed",
            }

        if not isinstance(ids, list) or not ids:
            return {
                "error": "ids must be a non-empty list",
            }

        try:
            ids = [int(record_id) for record_id in ids]
        except (TypeError, ValueError):
            return {
                "error": "ids must contain valid integers",
            }

        if len(ids) > 500:
            return {
                "error": "Maximum 500 ids per request",
            }

        scoped_domain = self._apply_company_scope(
            env,
            model,
            [("id", "in", ids)],
            company_ids,
        )
        records = env[model].sudo().search(
            scoped_domain,
            limit=500,
        ).read(
            fields=fields or [],
        )

        return {
            "success": True,
            "connection_id": connection.id,
            "model": model,
            "count": len(records),
            "records": records,
        }

    @http.route(
        "/api/dk-ai/read-group",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def read_group(
        self,
        model=None,
        domain=None,
        fields=None,
        groupby=None,
        offset=0,
        limit=100,
        orderby=None,
        lazy=True,
        company_ids=None,
    ):
        connection, env = self._get_connector_env(
            company_ids=company_ids,
        )

        if not connection:
            return {
                "error": "Unauthorized connector",
            }

        if not model:
            return {
                "error": "Model is required",
            }

        allowed_models = ALLOWED_MODELS

        if model not in allowed_models:
            return {
                "error": "Model is not allowed",
            }

        try:
            offset = max(int(offset or 0), 0)
        except (TypeError, ValueError):
            offset = 0

        try:
            limit = min(max(int(limit or 100), 1), 500)
        except (TypeError, ValueError):
            limit = 100

        scoped_domain = self._apply_company_scope(
            env,
            model,
            domain,
            company_ids,
        )

        try:
            records = env[model].sudo().read_group(
                scoped_domain,
                fields or [],
                groupby or [],
                offset=offset,
                limit=limit,
                orderby=orderby,
                lazy=bool(lazy),
            )
        except Exception as exc:
            _logger.warning("DK AI query failed: %s", exc)
            return {
                "error": "The query could not be completed.",
            }

        return {
            "success": True,
            "connection_id": connection.id,
            "model": model,
            "count": len(records),
            "records": records,
        }

    @http.route(
        "/api/dk-ai/search-count",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def search_count(self, model=None, domain=None, company_ids=None):
        connection, env = self._get_connector_env(
            company_ids=company_ids,
        )

        if not connection:
            return {
                "error": "Unauthorized connector",
            }

        if not model:
            return {
                "error": "Model is required",
            }

        allowed_models = ALLOWED_MODELS

        if model not in allowed_models:
            return {
                "error": "Model is not allowed",
            }

        try:
            scoped_domain = self._apply_company_scope(
                env,
                model,
                domain,
                company_ids,
            )

            count = env[model].sudo().search_count(
                scoped_domain,
            )
        except Exception as exc:
            _logger.warning("DK AI query failed: %s", exc)
            return {
                "error": "The query could not be completed.",
            }

        return {
            "success": True,
            "connection_id": connection.id,
            "model": model,
            "count": count,
        }

    def _apply_company_scope(self, env, model, domain, company_ids):
        # Scope queries to the effective companies of the connector user.
        effective_company_ids = (
            env.context.get("allowed_company_ids")
            or company_ids
            or []
        )

        if not effective_company_ids:
            return [("id", "=", False)]

        domain = domain or []

        if model == "res.company":
            return [
                ("id", "in", effective_company_ids),
            ] + domain

        model_obj = env[model]

        if "company_id" not in model_obj._fields:
            # Global/shared models need model-specific access policies.
            return domain

        # These models represent company-owned transactions or stock records.
        # Do not include company_id=False records in their results.
        strict_company_models = {
            "account.move",
            "account.move.line",
            "sale.order",
            "sale.order.line",
            "purchase.order",
            "purchase.order.line",
            "stock.move",
            "stock.quant",
            "stock.valuation.layer",
        }

        if model == "stock.quant":
            company_domain = [
                "|",
                ("company_id", "in", effective_company_ids),
                "&",
                ("company_id", "=", False),
                ("location_id.company_id", "in", effective_company_ids),
            ]
            return company_domain + domain

        if model in strict_company_models:
            company_domain = [
                ("company_id", "in", effective_company_ids),
            ]
        else:
            # Preserve legitimate shared records for non-transactional models.
            company_domain = [
                "|",
                ("company_id", "=", False),
                ("company_id", "in", effective_company_ids),
            ]

        return company_domain + domain

    @http.route(
        "/api/dk-ai/search-read",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def search_read(
        self,
        model=None,
        domain=None,
        fields=None,
        offset=0,
        limit=100,
        order=None,
        company_ids=None,
    ):
        connection, env = self._get_connector_env(
            company_ids=company_ids,
        )

        if not connection:
            return {
                "error": "Unauthorized connector",
            }

        if not model:
            return {
                "error": "Model is required",
            }

        allowed_models = SEARCH_READ_MODELS

        if model not in allowed_models:
            return {
                "error": "Model is not allowed",
            }

        try:
            offset = max(int(offset or 0), 0)
        except (TypeError, ValueError):
            offset = 0

        try:
            limit = min(max(int(limit or 100), 1), 500)
        except (TypeError, ValueError):
            limit = 100

        scoped_domain = self._apply_company_scope(
            env,
            model,
            domain,
            company_ids,
        )

        records = env[model].sudo().search_read(
            scoped_domain,
            fields=fields or [],
            offset=offset,
            limit=limit,
            order=order,
        )

        return {
            "success": True,
            "connection_id": connection.id,
            "model": model,
            "count": len(records),
            "records": records,
        }

    @http.route(
        "/api/dk-ai/pairing/complete",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def complete_pairing(self, **kwargs):
        """Complete a pending pairing from a trusted DK AI server callback."""
        payload = request.httprequest.get_json(silent=True) or {}
        pairing_code = payload.get("pairing_code")
        instance_uid = payload.get("instance_uid")

        def response(body, status=200):
            return http.Response(
                json.dumps(body),
                status=status,
                content_type="application/json; charset=utf-8",
            )

        if not isinstance(pairing_code, str) or not isinstance(instance_uid, str):
            return response(
                {"success": False, "message": "Pairing details are required."},
                400,
            )

        if not pairing_code or not instance_uid:
            return response(
                {"success": False, "message": "Pairing details are required."},
                400,
            )

        Connection = request.env["dk.ai.connection"].sudo()
        connection = Connection.search(
            [
                ("active", "=", True),
                ("status", "=", "pending"),
                ("pairing_code", "=", pairing_code.strip()),
            ],
            limit=1,
        )

        if not connection:
            return response(
                {"success": False, "message": "No matching pending pairing was found."},
                404,
            )

        if (
            not connection.pairing_expires_at
            or connection.pairing_expires_at < fields.Datetime.now()
        ):
            return response(
                {"success": False, "message": "The pairing code has expired."},
                410,
            )

        if not hmac.compare_digest(
            connection._get_instance_uid(),
            instance_uid,
        ):
            return response(
                {"success": False, "message": "The Odoo instance did not match."},
                403,
            )

        try:
            connection._exchange_pairing_credentials()
        except UserError:
            return response(
                {
                    "success": False,
                    "message": "Credential exchange failed. Retry from DK AI or use Exchange Credential in Odoo.",
                },
                502,
            )
        except Exception:
            return response(
                {
                    "success": False,
                    "message": "Odoo could not complete the credential exchange.",
                },
                500,
            )

        return response({"success": True, "message": "Odoo credential exchange completed."})

