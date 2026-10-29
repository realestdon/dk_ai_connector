{
    "name": "DK AI Connector",
    "version": "1.0.0",
    "summary": "Securely pair Odoo with a DK AI Platform server",
    "description": """
DK AI Connector
===============

Pairs an Odoo database with a DK AI Platform server so that dashboards
and AI assistants can read business data (sales, purchases, inventory,
invoices, CRM) over a read-only API.

Features
--------
- Pairing with a one-time code that expires after 10 minutes
- Connection status, test and revoke actions
- Read-only API limited to an allow-list of models
- Restricted to Odoo administrators

A DK AI Platform server is required to use this module.
    """,
    "author": "DK Business Solutions",
    "website": "https://dishonkadoh.com",
    "category": "Tools",
    "license": "LGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.access.csv",
        "views/dk_ai_connection_views.xml",
        "views/menu.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
}
