from datetime import timedelta

from odoo import api, fields, models

STEPS = ['estimate', 'locked', 'agreement', 'project', 'qc', 'deployed', 'closed']
G = 'sales_project_lifecycle.'
PROOF = 'UFJPT0Y='


class OtmDemoLoader(models.AbstractModel):
    """Drives a lead through the real workflow, as the real users.

    Used to create the demo data (so demo records are always valid) and by the end-to-end
    acceptance test. It performs no shortcut: every step is a normal business action.
    """
    _name = 'otm.demo.loader'
    _description = 'Lifecycle Demo / Scenario Driver'

    # ------------------------------------------------------------------ pipeline
    @api.model
    def drive(self, users, lead, upto='closed', customer=None, additional=15000, with_issue=True, review=True):
        """users: dict(exec, head, ph, dev, qc, fin, trainer, deployer) of res.users records."""
        env, u = self.env, users
        out = {'lead': lead}
        reach = STEPS.index(upto)
        today = fields.Date.context_today(self)
        customer = customer or lead.customer_id or env['res.partner'].create({'name': lead.name.split(' - ')[0],
                                                                               'email': 'customer@example.com'})
        lead.sudo().write({'customer_id': customer.id})
        L = lead.with_user(u['exec'])
        if lead.stage == 'new':
            L.action_contact()
            L.action_collect_requirement()
            L.action_demo()
            demo = env['otm.demo'].with_user(u['exec']).create({
                'lead_id': lead.id, 'demo_date': today + timedelta(days=1 + lead.id % 300), 'start_time': 10,
                'end_time': 11, 'demo_person_id': u['exec'].id})
            demo.with_user(u['exec']).action_confirm()
            demo.with_user(u['exec']).write({'notes': 'Demo done, customer interested'})
            demo.with_user(u['exec']).action_complete()
            L.action_estimate()
        L.action_negotiate()
        est = env['otm.estimate'].with_user(u['exec']).create({
            'lead_id': lead.id, 'customer_id': customer.id,
            'line_ids': [(0, 0, {'service_id': s.service_id.id, 'quantity': s.quantity})
                         for s in lead.sudo().service_line_ids]})
        est.line_ids.with_user(u['head']).write({'additional_amount': additional})
        est.with_user(u['exec']).action_submit()
        est.with_user(u['head']).action_send()
        est.with_user(u['head']).action_approve()
        out['estimate'] = est
        if reach < 1:
            return out
        deal = env['otm.deal'].browse(est.with_user(u['head']).action_lock_deal(reason='Approved')['res_id'])
        out['deal'] = deal
        if reach < 2:
            return out
        ag = env['otm.customer.agreement'].browse(deal.with_user(u['exec']).action_create_agreement()['res_id'])
        e = u['exec']
        ag.with_user(e).action_generate()
        ag.with_user(e).action_send()
        ag.with_user(e).write({'customer_acceptance': 'Accepted by email'})
        ag.with_user(e).action_accept()
        ag.with_user(e).write({'signed_document': PROOF, 'signed_date': today})
        ag.with_user(e).action_sign()
        ag.with_user(u['head']).action_complete()
        self._collect(ag.payment_ids.filtered(lambda p: p.trigger == 'advance'), u['fin'])
        out['agreement'] = ag
        if reach < 3:
            return out
        project = env['project.project'].browse(deal.with_user(u['head']).action_create_project()['res_id'])
        project.with_user(u['ph']).write({
            'user_id': u['ph'].id, 'otm_start_date': today, 'otm_developer_ids': [(6, 0, [u['dev'].id])],
            'otm_qc_user_id': u['qc'].id, 'otm_deploy_user_id': u['deployer'].id,
            'otm_trainer_id': u['trainer'].id})
        project.with_user(u['ph']).action_otm_start()
        out['project'] = project
        if reach < 4:
            return out
        task = env['project.task'].with_user(u['ph']).create({
            'name': 'Build ' + lead.name, 'project_id': project.id, 'user_ids': [(6, 0, [u['dev'].id])],
            'otm_acceptance_criteria': 'Works as agreed'})
        task.with_user(u['dev']).action_otm_start()
        task.with_user(u['dev']).write({'otm_progress': 100})
        task.with_user(u['dev']).action_otm_submit()
        task.with_user(u['ph']).action_otm_complete()
        qc = env['otm.qc'].browse(project.with_user(u['ph']).action_submit_qc()['res_id'])
        qc.with_user(u['qc']).action_start()
        if with_issue:
            issue = env['otm.qc.issue'].with_user(u['qc']).create({
                'title': 'Report totals are wrong', 'project_id': project.id, 'qc_id': qc.id, 'severity': 'critical'})
            qc.with_user(u['qc']).action_fail()
            issue.with_user(u['ph']).write({'assigned_developer_id': u['dev'].id})
            issue.with_user(u['ph']).action_assign()
            issue.with_user(u['dev']).write({'resolution_notes': 'Fixed the totals'})
            issue.with_user(u['dev']).action_fix()
            qc = env['otm.qc'].browse(project.with_user(u['ph']).action_submit_qc()['res_id'])
            qc.with_user(u['qc']).action_start()
            issue.with_user(u['qc']).action_pass()
        qc.with_user(u['qc']).action_pass()
        out['qc'] = qc
        if reach < 5:
            return out
        server = env['otm.client.server'].with_user(u['ph']).create({
            'name': 'Prod ' + lead.name, 'customer_id': customer.id, 'provider': 'Hetzner', 'domain': 'example.com',
            'hosting_start_date': today, 'hosting_expiry_date': today + timedelta(days=365),
            'ssl_expiry_date': today + timedelta(days=90), 'responsible_id': u['dev'].id,
            'credential_reference': 'vault://prod'})
        dep = env['otm.deployment'].with_user(u['ph']).create({
            'project_id': project.id, 'version': '1.0', 'server_id': server.id, 'backup_confirmed': True})
        dep.with_user(u['ph']).action_approve()
        dep.with_user(u['deployer']).action_deploy()
        dep.with_user(u['deployer']).action_verify()
        dep.with_user(u['deployer']).write({'customer_confirmation': True, 'verified_by': customer.name,
                                            'verification_date': today})
        dep.with_user(u['deployer']).action_complete()
        training = env['otm.training'].with_user(u['ph']).create({
            'project_id': project.id, 'training_type': 'online', 'date': today + timedelta(days=1 + lead.id % 300),
            'start_time': 10, 'end_time': 12, 'trainer_id': u['trainer'].id})
        training.with_user(u['trainer']).write({'topics': 'Daily use', 'participants': 'Staff',
                                                'customer_confirmation': True})
        training.with_user(u['trainer']).action_start()
        training.with_user(u['trainer']).action_complete()
        self._collect(deal.payment_ids.filtered(lambda p: p.trigger == 'after_training'), u['fin'])
        project.with_user(u['ph']).action_final_delivery()
        self._collect(deal.payment_ids.filtered(lambda p: p.trigger == 'final_delivery'), u['fin'])
        out.update(server=server, deployment=dep, training=training)
        if reach < 6:
            return out
        rev = env['otm.customer.review'].sudo().search([('project_id', '=', project.id)])
        if review:
            rev.with_user(u['exec']).write({
                'rating': '5', 'service_rating': '5', 'quality_rating': '4', 'support_rating': '5',
                'recommendation': 'yes', 'testimonial_permission': True, 'comments': 'Smooth project.'})
            rev.with_user(u['exec']).action_submit()
        project.with_user(u['ph']).action_close()
        out['review'] = rev
        return out

    @api.model
    def _collect(self, payments, finance):
        today = fields.Date.context_today(self) - timedelta(days=1)
        for pay in payments:
            pay.with_user(finance).write({'payment_reference': 'UTR-' + pay.name, 'payment_method': 'bank',
                                          'proof': PROOF, 'paid_date': today})
            pay.with_user(finance).action_confirm()

    # ------------------------------------------------------------------ demo data
    @api.model
    def load(self):
        """Called from demo/demo_lifecycle.xml (demo mode only)."""
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param('sales_project_lifecycle.demo_loaded'):
            return True
        env, ref = self.env, self.env.ref
        Users = env['res.users'].with_context(no_reset_password=True)

        def user(login, name, group):
            found = Users.search([('login', '=', login)])
            return found or Users.create({'name': name, 'login': login, 'password': login,
                                          'group_ids': [(6, 0, [ref(G + group).id])]})
        users = {
            'exec': ref(G + 'demo_user_exec_a1'), 'head': ref(G + 'demo_user_head_a'),
            'ph': user('project_head', 'Project Head', 'group_project_head'),
            'dev': user('developer_a', 'Developer A', 'group_developer'),
            'qc': user('qc_user', 'QC User', 'group_qc'),
            'fin': user('finance', 'Finance User', 'group_finance'),
            'trainer': user('trainer', 'Trainer', 'group_developer'),
            'deployer': user('deployer', 'Deployer', 'group_developer'),
        }
        crm = ref(G + 'demo_service_crm')
        Lead = env['otm.lead']

        def lead(owner, name, quality='warm'):
            rec = Lead.with_user(owner).create({'name': name, 'lead_quality': quality})
            rec.sudo().write({'requirement_description': 'Needs ' + crm.name,
                              'service_line_ids': [(0, 0, {'service_id': crm.id, 'quantity': 1})]})
            return rec

        # 1. a fully delivered and closed project -> its services stay active
        closed = lead(users['exec'], 'Acme Traders - CRM')
        self.drive(users, closed, 'closed')
        # 2. a project in QC, 3. a locked deal waiting for the agreement, 4. a lead at the estimate stage
        self.drive(users, lead(users['exec'], 'Bright Schools - CRM', 'hot'), 'qc', with_issue=False)
        users_a2 = {**users, 'exec': ref(G + 'demo_user_exec_a2')}
        self.drive(users_a2, lead(users_a2['exec'], 'City Clinic - CRM', 'hot'), 'locked')
        self.drive(users_a2, lead(users_a2['exec'], 'Green Mart - CRM'), 'estimate')
        # 5. client services nearing expiry (the daily job turns them into renewal opportunities)
        customer = closed.customer_id or env['otm.deal'].search([('lead_id', '=', closed.id)]).customer_id
        today = fields.Date.context_today(self)
        team = ref(G + 'demo_team_a')
        env['otm.client.service'].with_user(users['exec']).create({
            'customer_id': customer.id, 'service_name': 'Annual Hosting', 'service_kind': 'hosting',
            'amount': 20000, 'renewal_amount': 24000, 'billing_type': 'yearly', 'renewal_period': 'yearly',
            'start_date': today - timedelta(days=335), 'expiry_date': today + timedelta(days=30),
            'sales_team_id': team.id})
        env['otm.client.integration'].with_user(users['head']).create({
            'name': 'WhatsApp Business API', 'customer_id': customer.id, 'integration_type': 'whatsapp',
            'provider': 'Meta', 'setup_amount': 8000, 'recurring_amount': 1500, 'start_date': today,
            'expiry_date': today + timedelta(days=45), 'sales_team_id': team.id,
            'responsible_developer_id': users['dev'].id})
        ICP.set_param('sales_project_lifecycle.demo_loaded', '1')
        return True
