import hmac
import json
import logging

from odoo import fields, http
from odoo.api import Environment
from odoo.http import request
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Models the connector may read. Everything else is refused.
# Optional applications and the models they provide.
OPTIONAL_MODEL_MODULES = {
    "sale.order": ("sale_management", "Sales"),
    "sale.order.line": ("sale_management", "Sales"),
    "crm.lead": ("crm", "CRM"),
    "purchase.order": ("purchase", "Purchase"),
    "purchase.order.line": ("purchase", "Purchase"),
    "account.account": ("account", "Accounting"),
    "account.account.tag": ("account", "Accounting"),
    "account.journal": ("account", "Accounting"),
    "account.move": ("account", "Accounting"),
    "account.move.line": ("account", "Accounting"),
    "account.tax": ("account", "Accounting"),
    "account.partial.reconcile": ("account", "Accounting"),
    "stock.location": ("stock", "Inventory"),
    "stock.move": ("stock", "Inventory"),
    "stock.quant": ("stock", "Inventory"),
    "stock.valuation.layer": ("stock", "Inventory"),
    "mail.activity": ("mail", "Activities"),
}

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
        "/api/dk-ai/fields-get",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def fields_get(
        self,
        model=None,
        fields=None,
        attributes=None,
        company_ids=None,
    ):
        """Return field metadata for an allowed Odoo model."""

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

        allowed_models = set(ALLOWED_MODELS) | set(SEARCH_READ_MODELS)

        if model not in allowed_models:
            return {
                "error": "Model is not allowed",
            }

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

        try:
            metadata = env[model].sudo().fields_get(
                allfields=fields or None,
                attributes=attributes or None,
            )

            return {
                "success": True,
                "connection_id": connection.id,
                "model": model,
                "fields": metadata,
                "count": len(metadata),
            }

        except Exception as exc:
            _logger.warning(
                "DK AI fields_get failed for %s: %s",
                model,
                exc,
            )
            return {
                "error": "Field metadata could not be retrieved.",
            }

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

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

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

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

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

    def _map_invoice_domain_for_odoo13(self, env, model, domain):
        """
        Translate modern invoice domain fields to Odoo 13 account.move fields.
        """
        if model != "account.move":
            return domain

        model_fields = env[model]._fields

        field_mapping = {}

        if "move_type" not in model_fields and "type" in model_fields:
            field_mapping["move_type"] = "type"

        def payment_state_domain(operator, value):
            """
            Convert modern payment_state filters into Odoo 13 residual filters.
            """
            if operator == "in":
                states = value if isinstance(value, (list, tuple, set)) else [value]

                if not states:
                    return [("id", "=", False)]

                if set(states).issubset(set(("not_paid", "partial"))):
                    return [
                        "&",
                        ("state", "=", "posted"),
                        ("amount_residual", ">", 0),
                    ]

                if set(states) == set(("paid",)):
                    return [
                        "&",
                        ("state", "=", "posted"),
                        ("amount_residual", "=", 0),
                    ]

                if set(states).issubset(
                    set(("not_paid", "partial", "paid"))
                ):
                    return [("state", "=", "posted")]

            if operator == "=":
                if value == "paid":
                    return [
                        "&",
                        ("state", "=", "posted"),
                        ("amount_residual", "=", 0),
                    ]

                if value == "not_paid":
                    return [
                        "&",
                        "&",
                        ("state", "=", "posted"),
                        ("amount_residual", "=", "amount_total"),
                    ]

                if value == "partial":
                    return [
                        "&",
                        "&",
                        ("state", "=", "posted"),
                        ("amount_residual", ">", 0),
                        ("amount_residual", "<", "amount_total"),
                    ]

            raise ValueError(
                "Unsupported Odoo 13 payment_state domain: %s %s %s"
                % (operator, value, "")
            )

        def map_leaf(node):
            if not isinstance(node, (tuple, list)):
                return [node]

            if (
                len(node) >= 3
                and isinstance(node[0], str)
                and node[0] not in ("&", "|", "!")
            ):
                field_name, operator, value = node[:3]

                if field_name == "payment_state":
                    return payment_state_domain(operator, value)

                mapped = list(node)
                mapped[0] = field_mapping.get(field_name, field_name)

                return [tuple(mapped) if isinstance(node, tuple) else mapped]

            # Preserve prefix operators and flatten expanded leaf domains.
            result = []

            for item in node:
                if isinstance(item, (tuple, list)):
                    result.extend(map_leaf(item))
                else:
                    result.append(item)

            return result

        return map_leaf(domain or [])


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

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

        try:
            query_domain = self._map_invoice_domain_for_odoo13(
                env,
                model,
                domain or [],
            )

            scoped_domain = self._apply_company_scope(
                env,
                model,
                query_domain,
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

    def _model_availability_error(self, env, model):
        """Return an actionable error if an allowed model is unavailable."""
        if model in env.registry.models:
            return None

        module_info = OPTIONAL_MODEL_MODULES.get(model)
        module_name, app_name = module_info or (None, None)

        if module_name:
            module = env["ir.module.module"].sudo().search(
                [("name", "=", module_name)],
                limit=1,
            )

            if not module or module.state != "installed":
                message = (
                    f"The {app_name} application is not installed "
                    "in the connected Odoo database."
                )
                return {
                    "error": message,
                    "code": "ODOO_MODULE_NOT_INSTALLED",
                    "message": message,
                    "model": model,
                    "module": module_name,
                    "action": (
                        f"Install the {app_name} application in Odoo, "
                        "then retry."
                    ),
                }

        return {
            "error": f"The Odoo model '{model}' is unavailable.",
            "code": "ODOO_MODEL_UNAVAILABLE",
            "message": (
                f"The Odoo model '{model}' is unavailable even though "
                "its expected application may be installed."
            ),
            "model": model,
            "module": module_name,
            "action": (
                "Verify the installed Odoo applications and restart or "
                "upgrade the relevant addon if necessary."
            ),
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

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

        try:
            offset = max(int(offset or 0), 0)
        except (TypeError, ValueError):
            offset = 0

        try:
            limit = min(max(int(limit or 100), 1), 500)
        except (TypeError, ValueError):
            limit = 100

        query_domain = self._map_invoice_domain_for_odoo13(
            env,
            model,
            domain or [],
        )

        # Translate modern invoice output fields for Odoo 13.
        requested_fields = list(fields or [])
        query_fields = list(requested_fields)

        # Odoo 13 stock.quant compatibility.
        synthetic_available_quantity = (
            model == "stock.quant"
            and "available_quantity" in requested_fields
        )

        if synthetic_available_quantity:
            query_fields = [
                field_name
                for field_name in query_fields
                if field_name != "available_quantity"
            ]

            for field_name in ("quantity", "reserved_quantity"):
                if field_name not in env[model]._fields:
                    raise ValueError(
                        "Cannot calculate available_quantity: "
                        "stock.quant.%s is unavailable."
                        % field_name
                    )

                if field_name not in query_fields:
                    query_fields.append(field_name)
        synthetic_payment_state = (
            model == "account.move"
            and "payment_state" in requested_fields
        )

        model_field_names = env[model]._fields

        if model == "account.move":
            # Odoo 13 uses "type" instead of "move_type".
            if "move_type" in query_fields and "move_type" not in model_field_names:
                if "type" not in model_field_names:
                    raise ValueError(
                        "Neither move_type nor type exists on account.move."
                    )

                query_fields.remove("move_type")
                if "type" not in query_fields:
                    query_fields.append("type")

            # payment_state is not available on this Odoo 13 model.
            if synthetic_payment_state:
                query_fields = [
                    field_name
                    for field_name in query_fields
                    if field_name != "payment_state"
                ]

                required_fields = (
                    "state",
                    "amount_residual",
                    "amount_total",
                    "currency_id",
                )

                for field_name in required_fields:
                    if field_name not in model_field_names:
                        raise ValueError(
                            "Cannot calculate payment_state: "
                            "account.move.%s is unavailable."
                            % field_name
                        )

                    if field_name not in query_fields:
                        query_fields.append(field_name)

        scoped_domain = self._apply_company_scope(
            env,
            model,
            query_domain,
            company_ids,
        )

        records = env[model].sudo().search_read(
            scoped_domain,
            fields=query_fields,
            offset=offset,
            limit=limit,
            order=order,
        )

        if synthetic_available_quantity:
            for record in records:
                quantity = record.get("quantity") or 0.0
                reserved_quantity = record.get("reserved_quantity") or 0.0

                record["available_quantity"] = (
                    quantity - reserved_quantity
                )

                if "quantity" not in requested_fields:
                    record.pop("quantity", None)

                if "reserved_quantity" not in requested_fields:
                    record.pop("reserved_quantity", None)

        if model == "account.move":
            for record in records:
                # Restore the modern field name in the API response.
                if (
                    "move_type" in requested_fields
                    and "move_type" not in record
                    and "type" in record
                ):
                    record["move_type"] = record["type"]

                if synthetic_payment_state:
                    state = record.get("state")
                    residual = abs(record.get("amount_residual") or 0.0)
                    total = abs(record.get("amount_total") or 0.0)

                    currency_value = record.get("currency_id")
                    currency_id = (
                        currency_value[0]
                        if isinstance(currency_value, (list, tuple))
                        and currency_value
                        else False
                    )

                    rounding = 0.01
                    if currency_id:
                        currency = env["res.currency"].sudo().browse(
                            currency_id
                        )
                        if currency.exists():
                            rounding = currency.rounding or rounding

                    if state != "posted":
                        payment_state = "not_paid"
                    elif residual <= rounding / 2.0:
                        payment_state = "paid"
                    elif residual < total - rounding / 2.0:
                        payment_state = "partial"
                    else:
                        payment_state = "not_paid"

                    record["payment_state"] = payment_state

                    # Remove helper fields unless the caller requested them.
                    for helper_field in (
                        "state",
                        "amount_residual",
                        "amount_total",
                        "currency_id",
                    ):
                        if helper_field not in requested_fields:
                            record.pop(helper_field, None)

                if (
                    "move_type" in requested_fields
                    and "type" not in requested_fields
                ):
                    record.pop("type", None)

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

