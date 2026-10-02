from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

G = 'sales_project_lifecycle.'
OPEN_ESTIMATE = ('draft', 'internal_review', 'sent', 'negotiation')
PIPELINE_ESTIMATE = ('sent', 'negotiation', 'approved')
WON_STAGES = ('deal_locked', 'agreement', 'advance_pending', 'project', 'won')
ACTIVE_PROJECT = ('planning', 'in_progress', 'on_hold', 'delivered')
BOARD_COLUMNS = [
    ('requirement', 'Requirement'), ('development', 'Development'), ('internal_testing', 'Internal Testing'),
    ('qc', 'QC'), ('deployment', 'Deployment'), ('verification', 'Verification'),
    ('training', 'Training'), ('payment', 'Payment'), ('completed', 'Completed')]
TRACKER_STEPS = [
    ('lead', 'Lead'), ('demo', 'Demo'), ('estimate', 'Estimate'), ('deal_locked', 'Deal Locked'),
    ('agreement', 'Agreement'), ('advance', 'Advance'), ('development', 'Development'), ('qc', 'QC'),
    ('deployment', 'Deployment'), ('training', 'Training'), ('payment', 'Payment'), ('review', 'Review'),
    ('completed', 'Completed')]


class OtmDashboard(models.AbstractModel):
    """Read-only data provider for the OWL screens.

    Every query runs as the current user, so ACLs and record rules decide what is
    counted: nothing is fetched company-wide and filtered in JavaScript.
    """
    _name = 'otm.dashboard'
    _description = 'Lifecycle Dashboard Data'

    # ------------------------------------------------------------------ helpers
    @api.model
    def _role(self):
        user = self.env.user
        if user.has_group(G + 'group_lifecycle_admin'):
            return 'admin'
        if user.sudo().otm_headed_team_ids:
            return 'head'
        if user.has_group(G + 'group_project_head'):
            return 'project_head'
        if user.has_group(G + 'group_finance'):
            return 'finance'
        if user.has_group(G + 'group_sales_executive'):
            return 'executive'
        if user.has_group(G + 'group_developer') or user.has_group(G + 'group_qc'):
            return 'worker'
        return False

    @api.model
    def _can_see_amounts(self):
        user = self.env.user
        return user.has_group(G + 'group_sales_head') or user.has_group(G + 'group_finance')

    @api.model
    def _can_read(self, model):
        try:
            self.env[model].check_access('read')
            return True
        except AccessError:
            return False

    @api.model
    def _count(self, model, domain):
        if not self._can_read(model):
            return 0
        return self.env[model].with_context(active_test=False).search_count(domain)

    @api.model
    def _sum(self, model, field, domain):
        if not self._can_read(model):
            return 0.0
        rows = self.env[model].with_context(active_test=False)._read_group(domain, [], [f'{field}:sum'])
        return rows[0][0] or 0.0

    @api.model
    def _kpi(self, key, label, model, domain, kind='count', field=None, tone='default', name=None):
        value = self._count(model, domain) if kind == 'count' else self._sum(model, field, domain)
        return {'key': key, 'label': label, 'value': value, 'kind': kind, 'tone': tone,
                'action': {'name': name or label, 'res_model': model, 'domain': domain}}

    @api.model
    def _date_domain(self, filters, field):
        dom = []
        if filters.get('date_from'):
            dom.append((field, '>=', filters['date_from']))
        if filters.get('date_to'):
            dom.append((field, '<', str(fields.Date.to_date(filters['date_to']) + timedelta(days=1))))
        return dom

    @api.model
    def _scope(self, filters, team_field='sales_team_id', head_field='sales_head_id', person_field='salesperson_id'):
        dom = []
        if filters.get('team_id'):
            dom.append((team_field, '=', int(filters['team_id'])))
        if filters.get('head_id') and head_field:
            dom.append((head_field, '=', int(filters['head_id'])))
        if filters.get('executive_id') and person_field:
            dom.append((person_field, '=', int(filters['executive_id'])))
        return dom

    @api.model
    def _clean_filters(self, filters):
        filters = dict(filters or {})
        for key in ('team_id', 'head_id', 'executive_id', 'project_head_id', 'developer_id'):
            if filters.get(key):
                try:
                    filters[key] = int(filters[key])
                except (TypeError, ValueError):
                    filters.pop(key)
        for key in ('date_from', 'date_to'):
            if filters.get(key):
                try:
                    filters[key] = str(fields.Date.to_date(filters[key]))
                except Exception:
                    filters.pop(key)
        return filters

    # ------------------------------------------------------------------ main entry
    @api.model
    def get_dashboard(self, filters=None):
        role = self._role()
        if not role:
            raise AccessError(_("You have no access to the lifecycle dashboard."))
        filters = self._clean_filters(filters)
        if role != 'admin':
            # only the administrator may slice across teams; others are already row-restricted
            for key in ('team_id', 'head_id'):
                filters.pop(key, None)
        builder = getattr(self, f'_dash_{role}')
        data = builder(filters)
        data.update({'role': role, 'filters': filters, 'currency': self.env.company.currency_id.symbol,
                     'options': self._filter_options(role)})
        return data

    # ------------------------------------------------------------------ comfort feeds (Next.js / OWL)
    @api.model
    def get_notifications(self, limit=30):
        """Recent workflow events on records the user can see (record rules apply to the log),
        excluding the user's own actions."""
        if not self._role():
            return []
        since = fields.Datetime.now() - timedelta(days=14)
        logs = self.env['otm.transition.log'].search(
            [('date', '>=', since), ('user_id', '!=', self.env.user.id)], limit=min(int(limit or 30), 100))
        return [{
            'id': l.id, 'date': fields.Datetime.to_string(l.date), 'res_model': l.res_model, 'res_id': l.res_id,
            'title': l.record_name or '', 'by': l.user_id.name, 'action': l.action,
            'from': l.from_state or '', 'to': l.to_state or '', 'reason': l.reason or '',
        } for l in logs]

    @api.model
    def get_trends(self, months=6):
        """Monthly leads, won value and received money for the last N months (user's visible data)."""
        if not self._role():
            return {'labels': [], 'series': []}
        months = max(1, min(int(months or 6), 24))
        today = fields.Date.context_today(self)
        first = today.replace(day=1)
        starts = []
        y, m = first.year, first.month
        for _i in range(months):
            starts.append(first.replace(year=y, month=m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        starts.reverse()
        labels = [d.strftime('%b %y') for d in starts]
        idx = {d: i for i, d in enumerate(starts)}
        since = starts[0]

        def bucket(model, date_field, domain, measure):
            out = [0.0] * months
            if not self._can_read(model):
                return out
            rows = self.env[model]._read_group(
                domain + [(date_field, '>=', since)], [f'{date_field}:month'], [measure])
            for month_start, value in rows:
                d = fields.Date.to_date(month_start)
                if d in idx:
                    out[idx[d]] = value or 0
            return out

        series = [
            {'key': 'leads', 'name': _("New leads"), 'kind': 'count',
             'values': bucket('otm.lead', 'create_date', [], '__count')},
        ]
        if not self._can_see_amounts():  # money series are for Sales Heads, Finance and the Administrator
            return {'labels': labels, 'currency': self.env.company.currency_id.symbol, 'series': series}
        return {'labels': labels, 'currency': self.env.company.currency_id.symbol, 'series': series + [
            {'key': 'won', 'name': _("Won value"), 'kind': 'sum',
             'values': bucket('otm.deal', 'locked_date', [('status', '=', 'locked')], 'total_amount:sum')},
            {'key': 'received', 'name': _("Received"), 'kind': 'sum',
             'values': bucket('otm.deal.payment', 'paid_date', [('status', '=', 'received')], 'amount:sum')},
        ]}

    @api.model
    def get_targets(self):
        """This month's won value against each visible team's monthly target."""
        if not self._role() or not self._can_see_amounts() or not self._can_read('otm.sales.team'):
            return []
        start = fields.Date.context_today(self).replace(day=1)
        teams = self.env['otm.sales.team'].search([('target_amount', '>', 0)])
        if not teams:
            return []
        Deal = self.env['otm.deal'].sudo()
        won = {t.id: v for t, v in Deal._read_group(
            [('status', '=', 'locked'), ('locked_date', '>=', start), ('sales_team_id', 'in', teams.ids)],
            ['sales_team_id'], ['total_amount:sum'])}
        return [{
            'team': t.name, 'target': t.target_amount, 'achieved': won.get(t.id, 0.0),
            'percent': round(won.get(t.id, 0.0) * 100.0 / t.target_amount, 1),
        } for t in teams]

    @api.model
    def _filter_options(self, role):
        if role != 'admin':
            return {}
        sudo = self.env['res.users'].sudo()
        teams = self.env['otm.sales.team'].search([])
        return {
            'teams': [{'id': t.id, 'name': t.name} for t in teams],
            'heads': [{'id': u.id, 'name': u.name} for u in teams.mapped('head_id')],
            'executives': [{'id': u.id, 'name': u.name} for u in teams.mapped('member_ids')],
            'project_heads': [{'id': u.id, 'name': u.name} for u in sudo.search(
                [('group_ids', 'in', self.env.ref(G + 'group_project_head').ids)])],
        }

    # ------------------------------------------------------------------ roles
    @api.model
    def _lead_pipeline(self, domain):
        if not self._can_read('otm.lead'):
            return []
        rows = self.env['otm.lead']._read_group(domain, ['stage'], ['__count'])
        counts = dict(rows)
        labels = dict(self.env['otm.lead']._fields['stage']._description_selection(self.env))
        return [{'key': k, 'label': labels[k], 'value': counts.get(k, 0)}
                for k in labels if k not in ('lost',) and counts.get(k, 0)]

    @api.model
    def _dash_executive(self, filters):
        me = self.env.user.id
        today = fields.Date.context_today(self)
        mine = [('salesperson_id', '=', me)]
        Activity = self.env['mail.activity'].sudo()
        due = Activity.search([('user_id', '=', me), ('res_model', '=', 'otm.lead'),
                               ('date_deadline', '<=', today)]).mapped('res_id')
        kpis = [
            self._kpi('leads', _("My Leads"), 'otm.lead', mine + [('stage', 'not in', ('won', 'lost'))]),
            self._kpi('hot', _("My Hot Leads"), 'otm.lead', mine + [('lead_quality', '=', 'hot'),
                                                                   ('stage', 'not in', ('won', 'lost'))], tone='danger'),
            self._kpi('demos', _("My Demos"), 'otm.demo', mine + [('status', 'in', ('scheduled', 'confirmed'))]),
            self._kpi('estimates', _("My Estimates"), 'otm.estimate', mine + [('status', 'in', OPEN_ESTIMATE)]),
            self._kpi('followups', _("My Follow-ups"), 'otm.lead', mine + [('id', 'in', due)], tone='warning'),
            self._kpi('advance', _("My Pending Advance"), 'otm.deal.payment',
                      mine + [('trigger', '=', 'advance'), ('status', 'in', ('due', 'requested'))], tone='warning'),
            self._kpi('projects', _("My Active Projects"), 'project.project',
                      [('otm_is_lifecycle', '=', True), ('otm_salesperson_id', '=', me),
                       ('otm_state', 'in', ACTIVE_PROJECT)]),
            self._kpi('renewals', _("My Renewals"), 'otm.client.service',
                      [('responsible_user_id', '=', me), ('status', 'in', ('expiring', 'expired'))], tone='danger'),
        ]
        return {'title': _("My Dashboard"), 'kpis': kpis,
                'charts': [{'title': _("My pipeline"), 'rows': self._lead_pipeline(mine)}], 'tables': []}

    @api.model
    def _team_performance(self, domain_scope, by='salesperson_id'):
        """Rows per executive (head) or per team (admin): leads, won deals, won value."""
        Lead, Deal = self.env['otm.lead'], self.env['otm.deal']
        if not (self._can_read('otm.lead') and self._can_read('otm.deal')):
            return []
        leads = dict(Lead._read_group(domain_scope, [by], ['__count']))
        won = {k: (c, v) for k, c, v in Deal._read_group(
            domain_scope + [('status', '=', 'locked')], [by], ['__count', 'total_amount:sum'])}
        keys = list(dict.fromkeys(list(leads) + list(won)))
        rows = []
        for k in keys:
            rows.append({'name': k.display_name if k else _("Unassigned"), 'leads': leads.get(k, 0),
                         'won': won.get(k, (0, 0))[0], 'value': won.get(k, (0, 0))[1]})
        return sorted(rows, key=lambda r: -r['value'])

    @api.model
    def _dash_head(self, filters):
        teams = self.env.user.sudo().otm_headed_team_ids
        scope = [('sales_team_id', 'in', teams.ids)] + self._scope({'executive_id': filters.get('executive_id')}, person_field='salesperson_id', head_field=None)
        d = self._date_domain(filters, 'create_date')
        today = fields.Date.context_today(self)
        live = ('won', 'lost')
        kpis = [
            self._kpi('leads', _("Team Leads"), 'otm.lead', scope + d + [('stage', 'not in', live)]),
            self._kpi('hot', _("Team Hot Leads"), 'otm.lead', scope + d + [('lead_quality', '=', 'hot'), ('stage', 'not in', live)], tone='danger'),
            self._kpi('warm', _("Team Warm Leads"), 'otm.lead', scope + d + [('lead_quality', '=', 'warm'), ('stage', 'not in', live)]),
            self._kpi('demos', _("Team Demos"), 'otm.demo', scope + [('status', 'in', ('scheduled', 'confirmed'))]),
            self._kpi('estimates', _("Team Estimates"), 'otm.estimate', scope + [('status', 'in', OPEN_ESTIMATE)]),
            self._kpi('est_value', _("Team Estimated Value"), 'otm.estimate', scope + [('status', 'in', PIPELINE_ESTIMATE)], 'sum', 'total_amount'),
            self._kpi('won_value', _("Team Won Value"), 'otm.deal', scope + [('status', '=', 'locked')], 'sum', 'total_amount', tone='success'),
            self._kpi('lost', _("Team Lost"), 'otm.lead', scope + d + [('stage', '=', 'lost')], tone='danger'),
            self._kpi('advance', _("Team Pending Advance"), 'otm.deal.payment', scope + [('trigger', '=', 'advance'), ('status', 'in', ('due', 'requested'))], tone='warning'),
            self._kpi('projects', _("Team Active Projects"), 'project.project',
                      [('otm_is_lifecycle', '=', True), ('otm_sales_team_id', 'in', teams.ids), ('otm_state', 'in', ACTIVE_PROJECT)]),
            self._kpi('commission', _("Team Commission"), 'otm.sales.commission', scope[:1] + [('status', 'in', ('earned', 'approved', 'paid'))], 'sum', 'commission_amount'),
            self._kpi('wallet', _("Team Wallet Balance"), 'otm.sales.wallet', [('sales_head_id', '=', self.env.user.id)], 'sum', 'balance'),
            self._kpi('expiring', _("Expiring Services"), 'otm.client.service', scope[:1] + [('status', '=', 'expiring')], tone='warning'),
            self._kpi('renewals', _("Renewals"), 'otm.service.renewal', scope[:1] + [('status', 'in', ('follow_up', 'estimate_sent', 'approved', 'paid'))]),
        ]
        title = ', '.join(teams.mapped('name')) or _("My Team")
        return {'title': title.upper(), 'kpis': kpis,
                'charts': [{'title': _("Team pipeline"), 'rows': self._lead_pipeline(scope)}],
                'tables': [{'title': _("Team performance"), 'columns': [_("Executive"), _("Leads"), _("Won"), _("Value")],
                            'rows': self._team_performance(scope), 'money_col': 3}]}

    @api.model
    def _dash_admin(self, filters):
        s_lead = self._scope(filters)
        s_pay = self._scope(filters)
        d = self._date_domain(filters, 'create_date')
        today = fields.Date.context_today(self)
        proj = [('otm_is_lifecycle', '=', True)]
        if filters.get('team_id'):
            proj.append(('otm_sales_team_id', '=', filters['team_id']))
        if filters.get('head_id'):
            proj.append(('otm_sales_head_id', '=', filters['head_id']))
        if filters.get('executive_id'):
            proj.append(('otm_salesperson_id', '=', filters['executive_id']))
        if filters.get('project_head_id'):
            proj.append(('user_id', '=', filters['project_head_id']))
        if filters.get('developer_id'):
            proj.append(('otm_developer_ids', 'in', [filters['developer_id']]))
        pteam = [('project_id.' + c[0], c[1], c[2]) for c in proj[1:]]
        kpis = [
            self._kpi('leads', _("Total Leads"), 'otm.lead', s_lead + d),
            self._kpi('sales', _("Total Sales"), 'otm.deal', s_lead + [('status', '=', 'locked')] + d, 'sum', 'total_amount', tone='success'),
            self._kpi('est_value', _("Total Estimated Value"), 'otm.estimate', s_lead + [('status', 'in', PIPELINE_ESTIMATE)] + d, 'sum', 'total_amount'),
            self._kpi('received', _("Total Received"), 'otm.deal.payment', s_pay + [('status', '=', 'received')], 'sum', 'amount', tone='success'),
            self._kpi('outstanding', _("Outstanding"), 'otm.deal', s_lead + [('status', '=', 'locked')], 'sum', 'balance_due', tone='danger'),
            self._kpi('projects', _("Active Projects"), 'project.project', proj + [('otm_state', 'in', ACTIVE_PROJECT)]),
            self._kpi('qc', _("QC Pending"), 'otm.qc', pteam + [('status', 'in', ('pending', 'testing'))], tone='warning'),
            self._kpi('training', _("Training Pending"), 'otm.training', pteam + [('status', 'in', ('scheduled', 'in_progress'))], tone='warning'),
            self._kpi('final', _("Final Payment Pending"), 'otm.deal.payment', s_pay + [('trigger', '=', 'final_delivery'), ('status', 'in', ('pending', 'due', 'requested'))], tone='warning'),
            self._kpi('commission', _("Commission"), 'otm.sales.commission', [('status', 'in', ('earned', 'approved', 'paid'))] + ([('sales_team_id', '=', filters['team_id'])] if filters.get('team_id') else []), 'sum', 'commission_amount'),
            self._kpi('renewals', _("Service Renewals"), 'otm.client.service', [('status', 'in', ('expiring', 'expired'))] + ([('sales_team_id', '=', filters['team_id'])] if filters.get('team_id') else []), tone='danger'),
        ]
        return {'title': _("Company Dashboard"), 'kpis': kpis,
                'charts': [{'title': _("Pipeline"), 'rows': self._lead_pipeline(s_lead + d)}],
                'tables': [{'title': _("Team comparison"), 'columns': [_("Team"), _("Leads"), _("Won"), _("Value")],
                            'rows': self._team_performance(s_lead, by='sales_team_id'), 'money_col': 3}]}

    @api.model
    def _dash_project_head(self, filters):
        me = self.env.user.id
        proj = [('otm_is_lifecycle', '=', True)]
        today = fields.Date.context_today(self)
        kpis = [
            self._kpi('projects', _("Active Projects"), 'project.project', proj + [('otm_state', 'in', ACTIVE_PROJECT)]),
            self._kpi('planning', _("Planning"), 'project.project', proj + [('otm_state', '=', 'planning')]),
            self._kpi('hold', _("On Hold"), 'project.project', proj + [('otm_state', '=', 'on_hold')], tone='warning'),
            self._kpi('qc', _("QC Pending"), 'otm.qc', [('status', 'in', ('pending', 'testing'))], tone='warning'),
            self._kpi('issues', _("Open QC Issues"), 'otm.qc.issue', [('status', 'in', ('open', 'assigned', 'fixed', 'retest'))], tone='danger'),
            self._kpi('deployments', _("Deployments In Progress"), 'otm.deployment', [('status', 'in', ('pending', 'approved', 'deployed', 'verification'))]),
            self._kpi('training', _("Training Pending"), 'otm.training', [('status', 'in', ('scheduled', 'in_progress'))], tone='warning'),
            self._kpi('closed', _("Completed Projects"), 'project.project', proj + [('otm_state', '=', 'closed')], tone='success'),
        ]
        return {'title': _("Project Dashboard"), 'kpis': kpis, 'charts': [], 'tables': []}

    @api.model
    def _dash_finance(self, filters):
        P = 'otm.deal.payment'
        pend = ('due', 'requested')
        kpis = [
            self._kpi('adv', _("Advance Pending"), P, [('trigger', '=', 'advance'), ('status', 'in', pend)], tone='warning'),
            self._kpi('p30', _("30% Pending"), P, [('trigger', '=', 'after_training'), ('status', 'in', pend)], tone='warning'),
            self._kpi('final', _("Final Payment Pending"), P, [('trigger', '=', 'final_delivery'), ('status', 'in', pend)], tone='warning'),
            self._kpi('due_amt', _("Amount To Collect"), P, [('status', 'in', pend)], 'sum', 'amount', tone='danger'),
            self._kpi('received', _("Total Received"), P, [('status', '=', 'received')], 'sum', 'amount', tone='success'),
            self._kpi('outstanding', _("Outstanding"), 'otm.deal', [('status', '=', 'locked')], 'sum', 'balance_due', tone='danger'),
            self._kpi('renewal_pay', _("Renewal Payments Pending"), 'otm.service.renewal', [('status', '=', 'approved')], tone='warning'),
        ]
        return {'title': _("Finance Dashboard"), 'kpis': kpis, 'charts': [], 'tables': []}

    @api.model
    def _dash_worker(self, filters):
        me = self.env.user.id
        kpis = [
            self._kpi('tasks', _("My Open Tasks"), 'project.task', [('user_ids', 'in', [me]), ('otm_dev_status', 'in', ('not_started', 'in_progress'))]),
            self._kpi('submitted', _("Awaiting Review"), 'project.task', [('user_ids', 'in', [me]), ('otm_dev_status', '=', 'submitted')]),
            self._kpi('bugs', _("My Open Bugs"), 'otm.qc.issue', [('assigned_developer_id', '=', me), ('status', 'in', ('open', 'assigned', 'retest'))], tone='danger'),
            self._kpi('qc', _("My QC Rounds"), 'otm.qc', [('project_id.otm_qc_user_id', '=', me), ('status', 'in', ('pending', 'testing'))], tone='warning'),
            self._kpi('trainings', _("My Training Sessions"), 'otm.training', [('trainer_id', '=', me), ('status', 'in', ('scheduled', 'in_progress'))]),
        ]
        return {'title': _("My Work"), 'kpis': kpis, 'charts': [], 'tables': []}

    # ------------------------------------------------------------------ project board (§60)
    @api.model
    def _board_column(self, p):
        """Map a lifecycle project to a board column using facts the workflow already stores."""
        p = p.sudo()
        if p.otm_state == 'closed':
            return 'completed'
        if p.otm_state == 'planning':
            return 'requirement'
        if p.otm_state == 'delivered':
            return 'payment'
        qcs = p.otm_qc_ids.sorted('id')
        last_qc = qcs[-1:]
        deps = p.otm_deployment_ids.filtered(lambda d: d.status not in ('rollback',))
        if last_qc and last_qc.status == 'passed':
            if any(d.status == 'completed' for d in deps):
                required = p.otm_training_ids.filtered(lambda t: t.required and t.status != 'cancelled')
                return 'training' if required and not p.otm_training_done else 'payment'
            if any(d.status in ('deployed', 'verification') for d in deps):
                return 'verification'
            return 'deployment'
        if last_qc:
            return 'qc'
        tasks = p.task_ids.filtered(lambda t: t.otm_dev_status)
        if tasks and all(t.otm_dev_status in ('submitted', 'completed') for t in tasks):
            return 'internal_testing'
        return 'development'

    @api.model
    def _payment_status(self, p):
        pays = p.sudo().otm_deal_id.payment_ids.filtered(lambda x: x.status != 'cancelled')
        if not pays:
            return ''
        done = len(pays.filtered(lambda x: x.status == 'received'))
        return _("%(d)s of %(t)s payments received", d=done, t=len(pays))

    @api.model
    def get_project_board(self, filters=None):
        if not self._role():
            raise AccessError(_("You have no access to the project board."))
        filters = self._clean_filters(filters)
        dom = [('otm_is_lifecycle', '=', True)]
        if filters.get('team_id'):
            dom.append(('otm_sales_team_id', '=', filters['team_id']))
        if filters.get('project_head_id'):
            dom.append(('user_id', '=', filters['project_head_id']))
        qc_labels = {'none': '', 'pending': _("QC pending"), 'testing': _("QC testing"),
                     'passed': _("QC passed"), 'failed': _("QC failed")}
        columns = {k: {'key': k, 'label': v, 'cards': []} for k, v in BOARD_COLUMNS}
        for p in self.env['project.project'].search(dom, order='otm_state, id desc', limit=500):
            sp = p.sudo()
            if sp.otm_state == 'cancelled':
                continue
            columns[self._board_column(p)]['cards'].append({
                'id': p.id, 'name': p.name, 'customer': sp.partner_id.display_name or '',
                'team': sp.otm_sales_team_id.name or '', 'salesperson': sp.otm_salesperson_id.name or '',
                'project_head': sp.user_id.name or '', 'developers': ', '.join(sp.otm_developer_ids.mapped('name')),
                'deadline': str(sp.date) if sp.date else '', 'progress': round(sp.otm_progress),
                'delay_days': sp.otm_delay_days, 'state': sp.otm_state,
                'payment': self._payment_status(p), 'qc': qc_labels.get(sp.otm_qc_state, '')})
        for c in columns.values():
            c['count'] = len(c['cards'])
        return {'columns': list(columns.values())}

    # ------------------------------------------------------------------ stage indicator (§61)
    @api.model
    def lifecycle_tracker(self, lead):
        """[{key, label, status}] status in completed/current/pending/blocked (read with sudo: it only exposes progress)."""
        lead = lead.sudo()
        deals = lead.deal_ids.filtered(lambda d: d.status in ('locked', 'revision'))
        deal = deals[:1]
        project = deal.project_id
        qcs = project.otm_qc_ids.sorted('id')
        deps = project.otm_deployment_ids
        pays = deal.payment_ids.filtered(lambda x: x.status != 'cancelled')
        required = project.otm_training_ids.filtered(lambda t: t.required and t.status != 'cancelled')
        done = {
            'lead': True,
            'demo': bool(lead.demo_ids.filtered(lambda d: d.status == 'completed')) or bool(deal),
            'estimate': bool(lead.estimate_ids.filtered(lambda e: e.status == 'approved')) or bool(deal),
            'deal_locked': bool(deal.filtered(lambda d: d.status == 'locked')),
            'agreement': bool(deal.agreement_ids.filtered(lambda a: a.status in ('signed', 'completed'))),
            'advance': bool(pays.filtered(lambda x: x.trigger == 'advance' and x.status == 'received')),
            'development': bool(project and qcs),
            'qc': bool(qcs[-1:] and qcs[-1:].status == 'passed'),
            'deployment': bool(deps.filtered(lambda d: d.status == 'completed')),
            'training': bool(deps.filtered(lambda d: d.status == 'completed')) and (not required or project.otm_training_done),
            'payment': bool(pays) and all(x.status == 'received' for x in pays),
            'review': bool(project.otm_review_ids.filtered(lambda r: r.status == 'submitted')),
            'completed': project.otm_state == 'closed',
        }
        blocked = lead.stage == 'lost' or project.otm_state in ('on_hold', 'cancelled')
        n_demo = len(lead.demo_ids)
        n_est = len(lead.estimate_ids)
        detail = {
            'lead': lead.stage and dict(lead._fields['stage'].selection).get(lead.stage, '') or '',
            'demo': _("%(d)s of %(n)s demo(s) completed", d=len(lead.demo_ids.filtered(lambda d: d.status == 'completed')), n=n_demo) if n_demo else _("No demo yet"),
            'estimate': _("%(a)s of %(n)s estimate(s) approved", a=len(lead.estimate_ids.filtered(lambda e: e.status == 'approved')), n=n_est) if n_est else _("No estimate yet"),
            'deal_locked': deal.name if deal else _("Not locked"),
            'agreement': _("Signed") if done['agreement'] else _("Not signed"),
            'advance': _("Received") if done['advance'] else _("Awaiting advance"),
            'development': project.name if project else _("Project not created"),
            'qc': _("Passed") if done['qc'] else (_("%s QC round(s)", len(qcs)) if qcs else _("Not started")),
            'deployment': _("Completed") if done['deployment'] else (_("In progress") if deps else _("Not started")),
            'training': _("Done") if done['training'] else _("Pending"),
            'payment': _("%(r)s of %(n)s received", r=len(pays.filtered(lambda x: x.status == 'received')), n=len(pays)) if pays else _("No payments"),
            'review': _("Submitted") if done['review'] else _("Awaiting review"),
            'completed': _("Closed") if done['completed'] else _("Open"),
        }
        out, current_found = [], False
        for key, label in TRACKER_STEPS:
            if done[key]:
                status = 'completed'
            elif not current_found:
                current_found = True
                status = 'blocked' if blocked else 'current'
            else:
                status = 'pending'
            out.append({'key': key, 'label': label, 'status': status, 'detail': detail.get(key, '')})
        return out

    # ------------------------------------------------------------------ customer 360 (§55)
    @api.model
    def get_customer_360(self, partner_id):
        partner = self.env['res.partner'].browse(int(partner_id)).exists()
        if not partner or not self._role() and not self.env.user.has_group('base.group_system'):
            raise AccessError(_("You have no access to Customer 360."))
        partner.check_access('read')
        today = fields.Date.context_today(self)
        cur = self.env.company.currency_id.symbol

        def section(model, builder):
            if not self._can_read(model):
                return {'restricted': True}
            return builder(self.env[model].with_context(active_test=False))

        def sales(Lead):
            leads = Lead.search([('customer_id', '=', partner.id)])
            deals = self.env['otm.deal'].search([('customer_id', '=', partner.id)]) if self._can_read('otm.deal') else self.env['otm.deal']
            ests = self.env['otm.estimate'].search([('customer_id', '=', partner.id)]) if self._can_read('otm.estimate') else self.env['otm.estimate']
            return {'leads': len(leads), 'estimates': len(ests), 'deals': len(deals),
                    'deal_value': (sum(deals.filtered(lambda d: d.status == 'locked').mapped('total_amount'))
                                   if self._can_see_amounts() else None),
                    'teams': sorted(set(leads.mapped('sales_team_id.name') + deals.mapped('sales_team_id.name'))),
                    'heads': sorted(set(leads.mapped('sales_head_id.name') + deals.mapped('sales_head_id.name'))),
                    'executives': sorted(set(leads.mapped('salesperson_id.name') + deals.mapped('salesperson_id.name')))}

        def projects(Project):
            ps = Project.search([('otm_is_lifecycle', '=', True), ('partner_id', '=', partner.id)])
            return {'active': len(ps.filtered(lambda p: p.otm_state in ('in_progress', 'planning', 'delivered'))),
                    'completed': len(ps.filtered(lambda p: p.otm_state == 'closed')),
                    'delayed': len(ps.filtered(lambda p: p.otm_state not in ('closed', 'cancelled') and p.otm_delay_days > 0)),
                    'items': [{'id': p.id, 'name': p.name, 'state': dict(p._fields['otm_state'].selection)[p.otm_state],
                               'progress': round(p.otm_progress)} for p in ps[:10]]}

        def payments(Pay):
            if not self._can_see_amounts():
                return {'restricted': True}
            pays = Pay.search([('deal_id.customer_id', '=', partner.id), ('status', '!=', 'cancelled')])
            total = sum(pays.mapped('amount'))
            received = sum(pays.filtered(lambda x: x.status == 'received').mapped('amount'))
            return {'total': total, 'received': received, 'outstanding': total - received}

        def services(Svc):
            ss = Svc.search([('customer_id', '=', partner.id)])
            return {'active': len(ss.filtered(lambda x: x.status in ('active', 'renewed'))),
                    'expiring': len(ss.filtered(lambda x: x.status == 'expiring')),
                    'expired': len(ss.filtered(lambda x: x.status == 'expired')),
                    'items': [{'id': x.id, 'name': x.service_name, 'expiry': str(x.expiry_date or ''),
                               'status': x.status} for x in ss.sorted('expiry_date')[:10]]}

        def servers(Srv):
            ss = Srv.search([('customer_id', '=', partner.id)])
            return {'active': len(ss.filtered(lambda x: x.status == 'active')),
                    'items': [{'id': x.id, 'name': x.name, 'hosting_expiry': str(x.hosting_expiry_date or ''),
                               'ssl_expiry': str(x.ssl_expiry_date or '')} for x in ss[:10]]}

        def integrations(Int):
            ii = Int.search([('customer_id', '=', partner.id)])
            return {'active': len(ii.filtered(lambda x: x.status in ('active', 'expiring'))),
                    'items': [{'id': x.id, 'name': x.name, 'expiry': str(x.expiry_date or ''),
                               'renewal': str(x.renewal_date or '')} for x in ii[:10]]}

        def reviews(Rev):
            rr = Rev.search([('customer_id', '=', partner.id), ('status', '=', 'submitted')])
            return {'count': len(rr), 'rating': round(sum(rr.mapped('average_rating')) / len(rr), 2) if rr else 0,
                    'items': [{'id': r.id, 'rating': r.average_rating, 'comments': r.comments or ''} for r in rr[:5]]}

        return {
            'partner': {'id': partner.id, 'name': partner.display_name, 'email': partner.email or '',
                        'phone': partner.phone or '', 'city': partner.city or '',
                        'country': partner.country_id.name or ''},
            'currency': cur,
            'sales': section('otm.lead', sales),
            'projects': section('project.project', projects),
            'payments': section('otm.deal.payment', payments),
            'services': section('otm.client.service', services),
            'servers': section('otm.client.server', servers),
            'integrations': section('otm.client.integration', integrations),
            'reviews': section('otm.customer.review', reviews),
        }

    @api.model
    def search_customers(self, term):
        if not self._role():
            raise AccessError(_("You have no access to Customer 360."))
        return self.env['res.partner'].search_read(
            [('name', 'ilike', term or ''), ('is_company', 'in', (True, False))], ['name'], limit=10)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def action_otm_customer360(self):
        self.ensure_one()
        return {'type': 'ir.actions.client', 'tag': 'otm_customer360', 'name': _('Customer 360'),
                'params': {'partner_id': self.id}}
