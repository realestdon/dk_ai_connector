# -*- coding: utf --*-
# Copyright (C) 2026 Dishon Kadoh (<dishon.kadoh@gmail.com>).
import hmac
import json
import logging

from odoo import fields, http
from odoo.api import Environment
from odoo.http import request
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_round

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
    "account.invoice": ("account", "Accounting"),
    "account.move": ("account", "Accounting"),
    "account.move.line": ("account", "Accounting"),
    "account.tax": ("account", "Accounting"),
    "account.partial.reconcile": ("account", "Accounting"),
    "stock.location": ("stock", "Inventory"),
    "stock.move": ("stock", "Inventory"),
    "stock.quant": ("stock", "Inventory"),
    "mail.activity": ("mail", "Activities"),
}

ALLOWED_MODELS = {
    "ir.module.module",
    "account.account",
    "account.account.tag",
    "account.journal",
    "account.invoice",
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
}

# search_read additionally exposes activities.
SEARCH_READ_MODELS = ALLOWED_MODELS | {"mail.activity",
                                       "account.partial.reconcile"}


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
                    or len(requested_company_ids) != len(
                set(requested_company_ids))
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
            _logger.exception("DK AI query failed. Scoped domain: %r",
                              scoped_domain)
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

    def _map_legacy_invoice_field(self, field_name):
        """Map newer invoice fields to Odoo 11 account.invoice."""
        mapping = {
            "move_type": "type",
            "invoice_date": "date_invoice",
            "invoice_date_due": "date_due",
            "amount_residual": "residual",
        }
        return mapping.get(field_name, field_name)

    def _normalize_legacy_invoice_fields(self, fields):
        """Translate requested invoice fields."""
        if not fields:
            return fields

        return [
            self._map_legacy_invoice_field(field_name)
            for field_name in fields
        ]

    def _normalize_legacy_invoice_order(self, order):
        """Translate invoice sorting fields."""
        if not order:
            return order

        normalized_parts = []

        for item in order.split(","):
            item = item.strip()

            if not item:
                continue

            tokens = item.split()
            field_name = self._map_legacy_invoice_field(tokens[0])

            direction = tokens[1] if len(tokens) > 1 else ""

            if direction and direction.lower() not in ("asc", "desc"):
                raise ValueError(
                    "Invalid invoice sort direction: %s" % direction
                )

            normalized_parts.append(
                field_name + ((" " + direction) if direction else "")
            )

        return ", ".join(normalized_parts)

    def _normalize_legacy_partner_query(self, model, domain):
        """Translate Odoo 18 partner rank domains to Odoo 11."""
        if model != "res.partner" or not domain:
            return model, domain

        rank_fields = {
            "customer_rank": "customer",
            "supplier_rank": "supplier",
        }

        def translate(expression):
            if not isinstance(expression, (list, tuple)):
                return expression

            if (
                    len(expression) == 3
                    and isinstance(expression[0], basestring)
                    and expression[0] not in ("&", "|", "!")
                    and isinstance(expression[1], basestring)
            ):
                field_name, operator, value = expression

                if field_name not in rank_fields:
                    return expression

                legacy_field = rank_fields[field_name]

                def is_positive(item):
                    return item > 0

                if operator in (">", ">="):
                    result = is_positive(value)
                elif operator == "=":
                    result = is_positive(value)
                elif operator == "!=":
                    result = not is_positive(value)
                elif operator in ("<", "<="):
                    result = not is_positive(value)
                elif operator == "in":
                    result = any(is_positive(item) for item in value)
                elif operator == "not in":
                    result = not any(
                        is_positive(item) for item in value
                    )
                else:
                    raise ValueError(
                        "Unsupported partner rank operator: %s" % operator
                    )

                return (legacy_field, "=", bool(result))

            result = []

            for item in expression:
                translated = translate(item)

                if isinstance(translated, list):
                    result.extend(translated)
                else:
                    result.append(translated)

            return result

        translated_domain = translate(domain)

        _logger.info(
            "DK AI Odoo 11 compatibility: translated partner domain: %r",
            translated_domain,
        )

        return model, translated_domain

    def _map_legacy_crm_field(self, field_name):
        """Map newer CRM fields to Odoo 11 crm.lead fields."""
        mapping = {
            "expected_revenue": "planned_revenue",
        }
        return mapping.get(field_name, field_name)

    def _normalize_legacy_crm_query(self, model, domain):
        """Translate newer CRM domains to Odoo 11."""
        if model != "crm.lead" or not domain:
            return model, domain

        def translate(expression):
            if isinstance(expression, tuple):
                if (
                        len(expression) == 3
                        and isinstance(expression[0], basestring)
                ):
                    return (
                        self._map_legacy_crm_field(expression[0]),
                        expression[1],
                        expression[2],
                    )
                return expression

            if isinstance(expression, list):
                if (
                        len(expression) == 3
                        and isinstance(expression[0], basestring)
                        and expression[0] not in ("&", "|", "!")
                        and isinstance(expression[1], basestring)
                ):
                    return [
                        self._map_legacy_crm_field(expression[0]),
                        expression[1],
                        expression[2],
                    ]

                return [
                    translate(item)
                    if isinstance(item, (list, tuple))
                    else item
                    for item in expression
                ]

            return expression

        translated = translate(domain)

        _logger.info(
            "DK AI Odoo 11 compatibility: translated CRM domain: %r",
            translated,
        )

        return model, translated

    def _normalize_legacy_account_type_query(self, model, domain):
        """Translate modern account.move.line account_type domains to Odoo 11."""
        if model != "account.move.line" or not domain:
            return model, domain

        value_mapping = {
            "asset_receivable": "receivable",
            "liability_payable": "payable",
            "asset_cash": "liquidity",
            "liability_credit_card": "liquidity",
        }

        def translate_leaf(field_name, operator, value):
            if field_name != "account_type":
                return (field_name, operator, value)

            if operator not in ("=", "!=", "in", "not in"):
                raise ValueError(
                    "Unsupported account_type operator: %s" % operator
                )

            values = (
                list(value)
                if isinstance(value, (list, tuple))
                else [value]
            )

            translated_values = []

            for item in values:
                if item not in value_mapping:
                    raise ValueError(
                        "Odoo 11 cannot accurately represent account_type "
                        "value: %s" % item
                    )
                translated_values.append(value_mapping[item])

            translated_value = (
                translated_values
                if isinstance(value, (list, tuple))
                else translated_values[0]
            )

            return (
                "account_id.user_type_id.type",
                operator,
                translated_value,
            )

        def translate_domain(expression):
            if not isinstance(expression, (list, tuple)):
                return expression

            if (
                    len(expression) == 3
                    and isinstance(expression[0], basestring)
                    and expression[0] not in ("&", "|", "!")
                    and isinstance(expression[1], basestring)
            ):
                return translate_leaf(
                    expression[0],
                    expression[1],
                    expression[2],
                )

            result = []

            for item in expression:
                translated_item = translate_domain(item)

                if isinstance(translated_item, list):
                    result.extend(translated_item)
                else:
                    result.append(translated_item)

            return result

        translated = translate_domain(domain)

        _logger.info(
            "DK AI Odoo 11 compatibility: translated account_type domain: %r",
            translated,
        )

        return model, translated

    def _normalize_legacy_invoice_query(self, model, domain):
        """Translate newer invoice domains to Odoo 11 account.invoice."""
        if model != "account.move" or not domain:
            return model, domain

        def translate_leaf(field_name, operator, value):
            field_mapping = {
                "move_type": "type",
                "invoice_date": "date_invoice",
                "invoice_date_due": "date_due",
                "amount_residual": "residual",
            }

            field_name = field_mapping.get(field_name, field_name)

            if field_name == "state":
                if value == "posted":
                    value = "open"
                elif isinstance(value, (list, tuple)):
                    value = [
                        "open" if item == "posted" else item
                        for item in value
                    ]

            if field_name == "payment_state":
                values = (
                    list(value)
                    if isinstance(value, (list, tuple))
                    else [value]
                )

                has_unpaid = any(
                    item in ("not_paid", "partial")
                    for item in values
                )
                has_paid = "paid" in values

                if operator in ("=", "in"):
                    alternatives = []

                    if has_unpaid:
                        alternatives.append([
                            "&",
                            ("state", "=", "open"),
                            ("residual", ">", 0),
                        ])

                    if has_paid:
                        alternatives.append([
                            ("state", "=", "paid"),
                        ])

                    if not alternatives:
                        return [("id", "=", 0)]

                    result = alternatives[0]

                    for alternative in alternatives[1:]:
                        result = ["|"] + result + alternative

                    return result

                if operator in ("!=", "not in"):
                    if has_unpaid and not has_paid:
                        return [
                            "|",
                            ("state", "!=", "open"),
                            ("residual", "<=", 0),
                        ]

                    if has_paid and not has_unpaid:
                        return [("state", "!=", "paid")]

                    if has_unpaid and has_paid:
                        return [
                            "&",
                            ("state", "!=", "open"),
                            ("state", "!=", "paid"),
                        ]

                    return [("id", "!=", 0)]

                raise ValueError(
                    "Unsupported payment_state operator: %s" % operator
                )

            return (field_name, operator, value)

        def translate_domain(expression):
            if not isinstance(expression, (list, tuple)):
                return expression

            # A domain leaf.
            if (
                    len(expression) == 3
                    and isinstance(expression[0], basestring)
                    and expression[0] not in ("&", "|", "!")
                    and isinstance(expression[1], basestring)
            ):
                return translate_leaf(
                    expression[0],
                    expression[1],
                    expression[2],
                )

            result = []

            for item in expression:
                translated_item = translate_domain(item)

                if isinstance(translated_item, list):
                    result.extend(translated_item)
                else:
                    result.append(translated_item)

            return result

        translated = translate_domain(domain)

        _logger.info(
            "DK AI Odoo 11 compatibility: translated invoice domain: %r",
            translated,
        )

        return "account.invoice", translated

    def _legacy_valuation_adapter(
            self,
            env,
            domain=None,
            fields=None,
            offset=0,
            limit=100,
            order=None,
            company_ids=None,
    ):
        """
        Odoo 11 compatibility adapter for modern stock.valuation.layer.

        This returns eligible stock.move records, NOT genuine valuation
        layer records. IDs are stock.move IDs.
        """

        supported_fields = {
            "id",
            "stock_move_id",
            "product_id",
            "company_id",
            "description",
            "quantity",
            "unit_cost",
            "value",
            "remaining_qty",
            "remaining_value",
            "create_date",
        }

        source_fields = {
            "id": "id",
            "stock_move_id": "id",
            "product_id": "product_id",
            "company_id": "company_id",
            "description": "name",
            "value": "value",
            "remaining_qty": "remaining_qty",
            "remaining_value": "remaining_value",
            "create_date": "create_date",
        }

        requested_fields = list(fields or [
            "id",
            "stock_move_id",
            "product_id",
            "company_id",
            "description",
            "quantity",
            "unit_cost",
            "value",
            "remaining_qty",
            "remaining_value",
            "create_date",
        ])

        unsupported = [
            name for name in requested_fields
            if name not in supported_fields
        ]

        if unsupported:
            return {
                "error": (
                    "Unsupported Odoo 11 valuation compatibility "
                    "fields: {}"
                ).format(", ".join(unsupported)),
                "code": "UNSUPPORTED_VALUATION_FIELDS",
            }

        # Translate only domain fields whose semantics are equivalent.
        # quantity and unit_cost need computed-value filtering and are
        # deliberately rejected here.
        domain_mapping = {
            "stock_move_id": "id",
            "description": "name",
        }

        allowed_domain_fields = set([
            "id",
            "stock_move_id",
            "product_id",
            "company_id",
            "description",
            "value",
            "remaining_qty",
            "remaining_value",
            "create_date",
        ])

        def translate_domain(items):
            result = []

            for item in items or []:
                if isinstance(item, (list, tuple)):
                    if (
                            len(item) == 3
                            and isinstance(item[0], basestring)
                    ):
                        field_name, operator, value = item

                        if field_name in ("quantity", "unit_cost"):
                            raise ValueError(
                                "Domain filtering on '{}' is not "
                                "supported by the Odoo 11 adapter."
                                .format(field_name)
                            )

                        if field_name not in allowed_domain_fields:
                            raise ValueError(
                                "Unsupported valuation domain field: {}"
                                .format(field_name)
                            )

                        result.append((
                            domain_mapping.get(
                                field_name,
                                field_name,
                            ),
                            operator,
                            value,
                        ))
                    else:
                        result.append(translate_domain(item))
                else:
                    if item not in ("|", "&", "!"):
                        raise ValueError(
                            "Unsupported valuation domain expression."
                        )
                    result.append(item)

            return result

        try:
            translated_domain = translate_domain(domain or [])
        except ValueError as exc:
            return {
                "error": str(exc),
                "code": "UNSUPPORTED_VALUATION_DOMAIN",
            }

        # Every candidate must be completed before valuation is exposed.
        translated_domain = [
                                ("state", "=", "done"),
                            ] + translated_domain

        try:
            scoped_domain = self._apply_company_scope(
                env,
                "stock.move",
                translated_domain,
                company_ids,
            )

            Move = env["stock.move"].sudo()

            if order:
                order_mapping = {
                    "stock_move_id": "id",
                    "description": "name",
                }

                translated_order = []

                for order_part in order.split(","):
                    parts = order_part.strip().split()

                    if not parts:
                        continue

                    field_name = parts[0]
                    direction = (
                        parts[1].lower()
                        if len(parts) > 1
                        else "asc"
                    )

                    if field_name not in allowed_domain_fields:
                        return {
                            "error": (
                                "Unsupported valuation order field: {}"
                            ).format(field_name),
                            "code": "UNSUPPORTED_VALUATION_ORDER",
                        }

                    if direction not in ("asc", "desc"):
                        return {
                            "error": "Invalid order direction.",
                        }

                    source_name = order_mapping.get(
                        field_name,
                        field_name,
                    )

                    translated_order.append(
                        "{} {}".format(source_name, direction)
                    )

                order = ", ".join(translated_order)

            moves = Move.search(
                scoped_domain,
                order=order or "date desc, id desc",
            )

            records = []

            for move in moves:
                # Mirror the Odoo 11 valuation classification.
                # Dropship valuation is intentionally excluded.
                is_in = move._is_in()
                is_out = move._is_out()

                if not is_in and not is_out:
                    continue

                # Odoo 11 _run_valuation handles incoming before outgoing.
                # Use the same branch priority.
                signed_quantity = 0.0

                for move_line in move.move_line_ids:
                    if move_line.owner_id:
                        continue

                    source_valued = (
                        move_line.location_id._should_be_valued()
                    )
                    destination_valued = (
                        move_line.location_dest_id._should_be_valued()
                    )

                    if is_in:
                        qualifies = (
                                not source_valued and destination_valued
                        )
                        sign = 1.0
                    else:
                        qualifies = (
                                source_valued and not destination_valued
                        )
                        sign = -1.0

                    if not qualifies:
                        continue

                    if not move_line.product_uom_id:
                        continue

                    converted_qty = (
                        move_line.product_uom_id._compute_quantity(
                            move_line.qty_done,
                            move.product_id.uom_id,
                        )
                    )

                    signed_quantity += sign * converted_qty

                if not signed_quantity:
                    continue

                records.append((move, signed_quantity))

            total_count = len(records)

            try:
                offset = max(int(offset or 0), 0)
            except (TypeError, ValueError):
                offset = 0

            if limit is not None:
                try:
                    limit = max(int(limit), 0)
                except (TypeError, ValueError):
                    limit = 100

            if limit is None:
                selected = records[offset:]
            else:
                selected = records[offset:offset + limit]

            response_records = []

            for move, signed_quantity in selected:
                values = {}

                for field_name in requested_fields:
                    if field_name == "id":
                        values["id"] = move.id

                    elif field_name == "stock_move_id":
                        values["stock_move_id"] = [
                            move.id,
                            move.display_name,
                        ]

                    elif field_name == "product_id":
                        values["product_id"] = (
                            [move.product_id.id, move.product_id.display_name]
                            if move.product_id else False
                        )

                    elif field_name == "company_id":
                        values["company_id"] = (
                            [move.company_id.id, move.company_id.display_name]
                            if move.company_id else False
                        )

                    elif field_name == "description":
                        values["description"] = move.name

                    elif field_name == "quantity":
                        values["quantity"] = signed_quantity

                    elif field_name == "unit_cost":
                        values["unit_cost"] = abs(move.price_unit or 0.0)

                    elif field_name == "value":
                        values["value"] = move.value

                    elif field_name == "remaining_qty":
                        values["remaining_qty"] = move.remaining_qty

                    elif field_name == "remaining_value":
                        values["remaining_value"] = move.remaining_value

                    elif field_name == "create_date":
                        values["create_date"] = (
                                move.create_date or False
                        )

                response_records.append(values)

            return {
                "success": True,
                "records": response_records,
                "count": len(response_records),
                "total_count": total_count,
                "compatibility": {
                    "requested_model": "stock.valuation.layer",
                    "source_model": "stock.move",
                    "id_semantics": "stock.move ID, not valuation layer ID",
                    "dropship_valuation": "excluded",
                    "quantity": "calculated from valued stock move lines",
                    "value": (
                        "Odoo 11 stock.move.value; manual periodic "
                        "valuation may produce zero values"
                    ),
                },
            }

        except Exception:
            _logger.exception(
                "DK AI legacy valuation adapter failed. Domain: %r",
                scoped_domain if "scoped_domain" in locals() else domain,
            )
            return {
                "error": (
                    "The Odoo 11 valuation compatibility query "
                    "could not be completed."
                ),
                "code": "LEGACY_VALUATION_QUERY_FAILED",
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

        if model == "stock.valuation.layer":
            result = self._legacy_valuation_adapter(
                env,
                domain=domain,
                fields=["id"],
                offset=0,
                limit=None,
                company_ids=company_ids,
            )

            if not result.get("success"):
                return result

            return {
                "success": True,
                "connection_id": connection.id,
                "model": "stock.valuation.layer",
                "count": result["total_count"],
                "compatibility": result["compatibility"],
            }

        model, domain = self._normalize_legacy_invoice_query(
            model,
            domain,
        )

        allowed_models = ALLOWED_MODELS

        if model not in allowed_models:
            return {
                "error": "Model is not allowed",
            }

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

        model, domain = self._normalize_legacy_partner_query(
            model, domain
        )

        model, domain = self._normalize_legacy_crm_query(
            model, domain
        )
        model, domain = self._normalize_legacy_account_type_query(
            model, domain
        )

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
            _logger.exception("DK AI query failed. Scoped domain: %r",
                              scoped_domain)
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
                    "The {} application is not installed "
                    "in the connected Odoo database."
                ).format(app_name)
                return {
                    "error": message,
                    "code": "ODOO_MODULE_NOT_INSTALLED",
                    "message": message,
                    "model": model,
                    "module": module_name,
                    "action": (
                        "Install the {} application in Odoo, "
                        "then retry."
                    ).format(app_name),
                }

        return {
            "error": "The Odoo model '{}' is unavailable.".format(model),
            "code": "ODOO_MODEL_UNAVAILABLE",
            "message": (
                "The Odoo model '{}' is unavailable even though "
                "its expected application may be installed."
            ).format(model),
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
            "account.invoice",
            "account.move.line",
            "sale.order",
            "sale.order.line",
            "purchase.order",
            "purchase.order.line",
            "stock.move",
            "stock.quant",
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

    def _legacy_valuation_fields_get(self, allfields=None, attributes=None):
        """
        Return synthetic stock.valuation.layer metadata for Odoo 11.
        The synthetic model is backed by stock.move.
        """

        metadata = {
            "id": {
                "string": "ID",
                "type": "integer",
                "readonly": True,
            },
            "stock_move_id": {
                "string": "Stock Move",
                "type": "many2one",
                "relation": "stock.move",
                "readonly": True,
            },
            "product_id": {
                "string": "Product",
                "type": "many2one",
                "relation": "product.product",
                "readonly": True,
            },
            "company_id": {
                "string": "Company",
                "type": "many2one",
                "relation": "res.company",
                "readonly": True,
            },
            "description": {
                "string": "Description",
                "type": "char",
                "readonly": True,
            },
            "quantity": {
                "string": "Quantity",
                "type": "float",
                "readonly": True,
            },
            "unit_cost": {
                "string": "Unit Cost",
                "type": "float",
                "readonly": True,
            },
            "value": {
                "string": "Value",
                "type": "float",
                "readonly": True,
            },
            "remaining_qty": {
                "string": "Remaining Quantity",
                "type": "float",
                "readonly": True,
            },
            "remaining_value": {
                "string": "Remaining Value",
                "type": "float",
                "readonly": True,
            },
            "create_date": {
                "string": "Created On",
                "type": "datetime",
                "readonly": True,
            },
        }

        if allfields is not None:
            metadata = {
                name: field_metadata
                for name, field_metadata in metadata.items()
                if name in allfields
            }

        if attributes is not None:
            metadata = {
                name: {
                    key: value
                    for key, value in field_metadata.items()
                    if key in attributes
                }
                for name, field_metadata in metadata.items()
            }

        return metadata

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
            allfields=None,
            attributes=None,
            company_ids=None,
    ):
        connection, env = self._get_connector_env(
            company_ids=company_ids,
        )

        if not connection:
            return {"error": "Unauthorized connector"}

        if not model:
            return {"error": "Model is required"}

        # Validate requested field names.
        if allfields is not None and (
                not isinstance(allfields, list)
                or not all(isinstance(field, basestring) for field in allfields)
        ):
            return {"error": "allfields must be a list of field names"}

        # Validate requested metadata attributes.
        if attributes is not None and (
                not isinstance(attributes, list)
                or not all(
            isinstance(attribute, basestring)
            for attribute in attributes
        )
        ):
            return {"error": "attributes must be a list of strings"}

        # Odoo 11 synthetic valuation model.
        if model == "stock.valuation.layer":
            fields_metadata = self._legacy_valuation_fields_get(
                allfields=allfields,
                attributes=attributes,
            )

            return {
                "success": True,
                "connection_id": connection.id,
                "model": model,
                "fields": fields_metadata,
            }

        # Native Odoo models.
        if model not in ALLOWED_MODELS:
            return {"error": "Model is not allowed"}

        availability_error = self._model_availability_error(env, model)
        if availability_error:
            return availability_error

        if allfields is not None and (
                not isinstance(allfields, list)
                or not all(isinstance(field, basestring) for field in allfields)
        ):
            return {"error": "allfields must be a list of field names"}

        if attributes is not None and (
                not isinstance(attributes, list)
                or not all(
            isinstance(attribute, basestring) for attribute in attributes)
        ):
            return {"error": "attributes must be a list of strings"}

        try:
            fields_metadata = env[model].sudo().fields_get(
                allfields=allfields,
                attributes=attributes,
            )
        except Exception as exc:
            _logger.warning(
                "DK AI fields_get failed for model %s: %s",
                model,
                exc,
            )
            return {
                "error": "Field metadata could not be retrieved.",
            }

        return {
            "success": True,
            "connection_id": connection.id,
            "model": model,
            "fields": fields_metadata,
        }

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

        if model == "stock.valuation.layer":
            result = self._legacy_valuation_adapter(
                env,
                domain=domain,
                fields=fields,
                offset=offset,
                limit=limit,
                order=order,
                company_ids=company_ids,
            )

            if not result.get("success"):
                return result

            result["connection_id"] = connection.id
            result["model"] = "stock.valuation.layer"
            return result

        model, domain = self._normalize_legacy_invoice_query(
            model,
            domain,
        )

        allowed_models = SEARCH_READ_MODELS

        _logger.warning(
            "DK AI MODEL VALIDATION: endpoint=search_read "
            "model=%r allowed=%r",
            model,
            model in allowed_models,
        )

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

        model, domain = self._normalize_legacy_partner_query(model, domain)
        model, domain = self._normalize_legacy_crm_query(
            model, domain
        )
        model, domain = self._normalize_legacy_account_type_query(
            model, domain
        )

        # Odoo 11 CRM compatibility: fields and sorting.
        crm_original_fields = None

        if model == "crm.lead":
            crm_original_fields = list(fields) if fields else []

            if fields:
                fields = [
                    self._map_legacy_crm_field(field_name)
                    for field_name in fields
                ]

            if order:
                normalized_order = []

                for item in order.split(","):
                    item = item.strip()

                    if not item:
                        continue

                    tokens = item.split()
                    field_name = self._map_legacy_crm_field(
                        tokens[0]
                    )

                    direction = (
                        tokens[1] if len(tokens) > 1 else ""
                    )

                    normalized_order.append(
                        field_name
                        + ((" " + direction) if direction else "")
                    )

                order = ", ".join(normalized_order)

            _logger.info(
                "DK AI Odoo 11 CRM search_read: fields=%r order=%r",
                fields,
                order,
            )

        scoped_domain = self._apply_company_scope(
            env,
            model,
            domain,
            company_ids,
        )

        # Odoo 11 partner compatibility: rank fields.
        partner_original_fields = None

        if model == "res.partner" and fields:
            partner_original_fields = list(fields)

            partner_field_mapping = {
                "customer_rank": "customer",
                "supplier_rank": "supplier",
            }

            fields = list(dict.fromkeys([
                partner_field_mapping.get(field_name, field_name)
                for field_name in fields
            ]))

        # Odoo 11 stock.quant compatibility.
        quant_original_fields = None
        include_quant_available_quantity = False

        if model == "stock.quant" and fields:
            quant_original_fields = list(fields)
            include_quant_available_quantity = (
                    "available_quantity" in fields
            )

            fields = [
                "quantity" if field_name == "available_quantity"
                else field_name
                for field_name in fields
            ]

            if include_quant_available_quantity:
                if "quantity" not in fields:
                    fields.append("quantity")

                if "reserved_quantity" not in fields:
                    fields.append("reserved_quantity")

            # Remove duplicate ORM fields while preserving order.
            fields = list(dict.fromkeys(fields))

        # Odoo 11 product compatibility.
        product_original_fields = None
        include_free_qty = False

        if model == "product.product" and fields:
            product_original_fields = list(fields)
            include_free_qty = "free_qty" in fields

            fields = [
                "stock_value" if field_name == "total_value"
                else field_name
                for field_name in fields
                if field_name != "free_qty"
            ]

        # Odoo 11 invoice compatibility.
        requested_fields = list(fields) if fields else fields

        include_payment_state = bool(
            model == "account.invoice"
            and fields
            and "payment_state" in fields
        )

        requested_mapped_fields = None

        if model == "account.invoice":
            if fields:
                # payment_state does not exist in Odoo 11.
                # Exclude it from the actual ORM read.
                requested_mapped_fields = [
                    self._map_legacy_invoice_field(field_name)
                    for field_name in fields
                    if field_name != "payment_state"
                ]

                fields = list(requested_mapped_fields)

                # Fetch helper fields to calculate payment_state.
                if include_payment_state:
                    for helper_field in (
                            "state",
                            "residual",
                            "amount_total",
                    ):
                        if helper_field not in fields:
                            fields.append(helper_field)

            order = self._normalize_legacy_invoice_order(order)

            _logger.info(
                "DK AI Odoo 11 invoice search_read: fields=%r order=%r",
                fields,
                order,
            )

        records = env[model].sudo().search_read(
            scoped_domain,
            fields=fields or [],
            offset=offset,
            limit=limit,
            order=order,
        )

        # Restore Odoo 18/19 invoice field names in API response.
        if model == "account.invoice" and requested_fields:
            for record in records:

                if include_payment_state:
                    state = record.get("state")
                    residual = float(record.get("residual") or 0.0)
                    amount_total = float(
                        record.get("amount_total") or 0.0
                    )

                    if state == "paid" or residual <= 0:
                        payment_state = "paid"
                    elif residual < amount_total:
                        payment_state = "partial"
                    else:
                        payment_state = "not_paid"

                    record["payment_state"] = payment_state

                # Restore original field names requested by the client.
                for original_field in requested_fields:
                    mapped_field = self._map_legacy_invoice_field(
                        original_field
                    )

                    if mapped_field != original_field:
                        record[original_field] = record.get(mapped_field)

                # Remove fields fetched only for payment_state calculation.
                if include_payment_state:
                    for helper_field in (
                            "state",
                            "residual",
                            "amount_total",
                    ):
                        if helper_field not in requested_mapped_fields:
                            record.pop(helper_field, None)

        # Restore Odoo 18/19 partner rank fields.
        if model == "res.partner" and partner_original_fields:
            for record in records:
                if "customer_rank" in partner_original_fields:
                    record["customer_rank"] = (
                        1 if record.get("customer") else 0
                    )

                if "supplier_rank" in partner_original_fields:
                    record["supplier_rank"] = (
                        1 if record.get("supplier") else 0
                    )

                # Remove Boolean helper fields unless originally requested.
                if "customer_rank" in partner_original_fields:
                    if "customer" not in partner_original_fields:
                        record.pop("customer", None)

                if "supplier_rank" in partner_original_fields:
                    if "supplier" not in partner_original_fields:
                        record.pop("supplier", None)

        # Restore Odoo 18/19 stock.quant available_quantity.
        if (
                model == "stock.quant"
                and include_quant_available_quantity
        ):
            for record in records:
                quantity = float(record.get("quantity") or 0.0)
                reserved = float(
                    record.get("reserved_quantity") or 0.0
                )

                record["available_quantity"] = quantity - reserved

                # Remove fields fetched only as calculation helpers.
                if "quantity" not in quant_original_fields:
                    record.pop("quantity", None)

                if "reserved_quantity" not in quant_original_fields:
                    record.pop("reserved_quantity", None)

        # Calculate Odoo 11 free_qty using reserved stock quantities.
        if model == "product.product" and include_free_qty and records:

            product_ids = [
                record["id"]
                for record in records
            ]

            product_model = env["product.product"].sudo()
            quant_model = env["stock.quant"].sudo()

            # Use exactly the same location domain as Odoo 11
            # product quantity computation.
            quant_location_domain = (
                product_model._get_domain_locations()[0]
            )

            quant_domain = [
                               ("product_id", "in", product_ids),
                           ] + quant_location_domain

            # Preserve lot, owner and package restrictions.
            stock_context = env.context

            for context_key, quant_field in (
                    ("lot_id", "lot_id"),
                    ("owner_id", "owner_id"),
                    ("package_id", "package_id"),
            ):
                context_value = stock_context.get(context_key)

                if context_value is not None:
                    quant_domain.append((
                        quant_field,
                        "=",
                        context_value,
                    ))

            quant_groups = quant_model.read_group(
                quant_domain,
                [
                    "product_id",
                    "quantity",
                    "reserved_quantity",
                ],
                ["product_id"],
                lazy=False,
            )

            free_qty_by_product = {}

            for group in quant_groups:
                product_id = group["product_id"][0]

                quantity = float(
                    group.get("quantity") or 0.0
                )

                reserved_quantity = float(
                    group.get("reserved_quantity") or 0.0
                )

                free_qty_by_product[product_id] = (
                        quantity - reserved_quantity
                )

            products = product_model.browse(product_ids)

            product_rounding = {}

            for product in products:
                product_rounding[product.id] = (
                    product.uom_id.rounding
                )

            for record in records:
                product_id = record["id"]

                free_qty = free_qty_by_product.get(
                    product_id,
                    0.0,
                )

                rounding = product_rounding.get(
                    product_id,
                    0.01,
                )

                record["free_qty"] = float_round(
                    free_qty,
                    precision_rounding=rounding,
                )

        # Restore Odoo 18/19 product field names in API response.
        if model == "product.product" and product_original_fields:
            for record in records:
                if "total_value" in product_original_fields:
                    record["total_value"] = record.get("stock_value")

                    if "stock_value" not in product_original_fields:
                        record.pop("stock_value", None)

        # Restore newer CRM field names in the API response.
        if model == "crm.lead" and crm_original_fields:
            for record in records:
                if "expected_revenue" in crm_original_fields:
                    record["expected_revenue"] = record.get(
                        "planned_revenue"
                    )

                    if "planned_revenue" not in crm_original_fields:
                        record.pop("planned_revenue", None)

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

        if not isinstance(pairing_code, str) or not isinstance(instance_uid,
                                                               str):
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
                {"success": False,
                 "message": "No matching pending pairing was found."},
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
                {"success": False,
                 "message": "The Odoo instance did not match."},
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

        return response(
            {"success": True, "message": "Odoo credential exchange completed."})
