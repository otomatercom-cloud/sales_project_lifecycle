# Sales & Project Lifecycle Management (`sales_project_lifecycle`)

Odoo 19 Community · License OPL-1 · Author Otomater · Model prefix `otm.`

Lead → demo → requirement → estimate → customization → deal lock → commission / wallet → agreement →
50% advance → project → development → QC → deployment → customer verification → training → 30% →
final delivery → 20% → review → closure → client services / servers / integrations → renewals.

## Installation
1. Copy the `sales_project_lifecycle` folder into an addons path (e.g. `/opt/odoo/custom-addons`).
2. Restart Odoo, enable developer mode, **Apps → Update Apps List**, search *Sales & Project Lifecycle*.
3. Install (depends on `base`, `mail`, `project`). Command line:
   `odoo-bin -d <db> -i sales_project_lifecycle [--with-demo]`
4. Create the Sales Teams (**Sales & Project Lifecycle → Configuration → Sales Teams**), assign Sales Heads and
   Executives, then give each user one of the *Lifecycle* groups (Settings → Users).
5. Review **Settings → Sales & Project Lifecycle** (discount limits, reminder days, follow-up days) and the
   configuration menus (payment schedule 50/30/20, commission rules, project stage templates).

## Upgrade
`odoo-bin -d <db> -u sales_project_lifecycle`
Record rules, e-mail templates and cron jobs are `noupdate` data: after a release that changes them, either
re-import the changed XML records or update the matching rows in *Settings → Technical*.

## Roles
`group_lifecycle_admin`, `group_sales_head`, `group_sales_executive`, `group_project_head`, `group_developer`,
`group_qc`, `group_finance`, `group_finance_manager`. All access is enforced by ACLs and record rules on the
server; the OWL screens only receive data the logged-in user may read.

## Demo data (`--with-demo`)
Logins equal passwords: `head_a`, `exec_a1`, `exec_a2`, `head_b`, `exec_b1`, `project_head`, `developer_a`,
`qc_user`, `finance`, `trainer`, `deployer`. Four sample leads cover a closed project (with review and client
services), a project in QC, a locked deal and an open estimate, plus an expiring service and integration. They
are created through the real workflow, so they are always valid.

## Scheduled actions
* Estimate expiry (daily)
* Service / integration / server expiry reminders (daily; reminder days configurable)
* Lifecycle checks: demo reminders, estimate follow-up, pending agreement, pending payment, stage deadline /
  overdue, training (daily; every notification is logged so it is never duplicated)

## Tests
`odoo-bin -d <db> -i sales_project_lifecycle --with-demo --test-enable --test-tags /sales_project_lifecycle --stop-after-init`

## Known limitations
* A Project Head can **read** every lifecycle project (the group implies Odoo's *Project Manager* group, whose rule
  is company-wide). Changing a project, its stages, QC, deployment or training is still limited to that project's own Project Head
  (or an Administrator). Restricting read access per project would need the group not to imply Project Manager.
* Money is stored in the company currency only (no multi-currency conversion).
* Customer e-mails are queued through Odoo's normal mail queue; an outgoing mail server must be configured.
* The estimate and agreement PDFs need `wkhtmltopdf`; the HTML rendering is tested, the PDF binary is not.
* The customer review is recorded by the salesperson / Sales Head / Project Head on the customer's behalf (no portal page).
* Reports are standard pivot / graph / list views with filters (Excel export through the list view); there is no
  separate BI layer.
