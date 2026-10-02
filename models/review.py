from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
RATING = [('1', '1 - Very poor'), ('2', '2 - Poor'), ('3', '3 - Average'), ('4', '4 - Good'), ('5', '5 - Excellent')]
REVIEW_MATRIX = {'submit': {'from': ('requested',), 'to': 'submitted'}}
RATING_FIELDS = ['rating', 'service_rating', 'quality_rating', 'support_rating']


class OtmCustomerReview(models.Model):
    _name = 'otm.customer.review'
    _description = 'Customer Review'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = REVIEW_MATRIX

    def _otm_email_partner(self):
        return self.customer_id

    name = fields.Char(readonly=True, copy=False, default=lambda s: _('New'))
    project_id = fields.Many2one('project.project', required=True, index=True, ondelete='restrict', readonly=True)
    customer_id = fields.Many2one('res.partner', required=True, index=True, readonly=True)
    sales_team_id = fields.Many2one('otm.sales.team', index=True, readonly=True)
    sales_head_id = fields.Many2one('res.users', index=True, readonly=True)
    salesperson_id = fields.Many2one('res.users', index=True, readonly=True)
    status = fields.Selection([('requested', 'Requested'), ('submitted', 'Submitted')],
                              default='requested', required=True, tracking=True, index=True, copy=False)
    requested_date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    rating = fields.Selection(RATING, string='Overall Rating')
    service_rating = fields.Selection(RATING)
    quality_rating = fields.Selection(RATING)
    support_rating = fields.Selection(RATING)
    average_rating = fields.Float(compute='_compute_average', store=True, digits=(16, 2))
    recommendation = fields.Selection([('yes', 'Would recommend'), ('maybe', 'Maybe'), ('no', 'Would not recommend')])
    comments = fields.Text()
    submitted_date = fields.Datetime(readonly=True, copy=False)
    submitted_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    testimonial_permission = fields.Boolean(string='Testimonial Permission')
    published = fields.Boolean(readonly=True, copy=False, tracking=True)

    @api.depends('rating', 'service_rating', 'quality_rating', 'support_rating')
    def _compute_average(self):
        for r in self:
            vals = [int(r[f]) for f in RATING_FIELDS if r[f]]
            r.average_rating = sum(vals) / len(vals) if vals else 0.0

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("Review requests are created automatically after the final payment."))
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.customer.review') or _('New')
        reviews = super().create(vals_list)
        reviews._otm_send_mail('sales_project_lifecycle.mail_review_request')
        return reviews

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if {'status', 'published', 'submitted_date', 'submitted_by_id', 'project_id',
                    'customer_id', 'requested_date'} & vals.keys():
                raise UserError(_("These fields are managed by the workflow."))
            for r in self:
                if r.status == 'submitted':
                    raise UserError(_("A submitted review cannot be changed."))
                r._otm_check_actor('edit')
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Reviews cannot be deleted."))
        return super().unlink()

    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        if user not in (self.salesperson_id | self.sales_head_id | self.project_id.sudo().user_id):
            raise AccessError(_("Only the salesperson, the Sales Head or the Project Head can record the review."))

    @api.constrains('rating', 'service_rating', 'quality_rating', 'support_rating')
    def _check_ratings(self):
        for r in self:
            if r.status == 'submitted' and not all(r[f] for f in RATING_FIELDS):
                raise ValidationError(_("All four ratings are required."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = [_("The '%s' is required.", self._fields[f].string) for f in RATING_FIELDS if not self[f]]
        if not self.recommendation:
            missing.append(_("The recommendation is required."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        for r in self:
            r.with_context(otm_transition=True).write({
                'submitted_date': fields.Datetime.now(), 'submitted_by_id': self.env.user.id})

    def action_submit(self):
        return self._otm_do_transition('submit')

    def action_publish(self):
        for r in self:
            user = self.env.user
            if not (self.env.su or user.has_group(ADMIN) or user == r.sales_head_id):
                raise AccessError(_("Only the Sales Head or an Administrator can publish a review."))
            if r.status != 'submitted':
                raise UserError(_("Only a submitted review can be published."))
            if not r.testimonial_permission:
                raise UserError(_("The customer has not given testimonial permission."))
            r.with_context(otm_transition=True).write({'published': True})
        return True

    def action_unpublish(self):
        for r in self:
            user = self.env.user
            if not (self.env.su or user.has_group(ADMIN) or user == r.sales_head_id):
                raise AccessError(_("Only the Sales Head or an Administrator can unpublish a review."))
            r.with_context(otm_transition=True).write({'published': False})
        return True
