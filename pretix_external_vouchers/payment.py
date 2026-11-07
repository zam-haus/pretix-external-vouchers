import re
from collections import OrderedDict
from datetime import datetime, time, timedelta
from decimal import Decimal
from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.timezone import make_aware, now
from django.utils.translation import gettext_lazy as _
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import BasePaymentProvider, ManualPayment
from pretix.base.services.payment import compute_payment_deadline

IDENTIFIER = "external_voucher"
SESSION_KEY = f"payment_{IDENTIFIER}_answer"
META_KEY = "external_voucher_answer"
DEFAULT_ANSWER_REGEX = r"[0-9a-zA-Z]{3}-[0-9a-zA-Z]{3}-[0-9a-zA-Z]{3}"


def _compile_full(pattern):
    """Compile ``pattern`` so that it has to match the whole answer."""
    return re.compile(rf"^(?:{pattern})\Z")


def validate_regex(value):
    if value:
        try:
            _compile_full(value)
        except re.error as e:
            raise ValidationError(_("Invalid regular expression: %s") % e)


def _end_of(obj, tz):
    """
    End of an event or subevent. Without an end date, the end of the day after
    the start (in the event's timezone).
    """
    if obj.date_to:
        return obj.date_to
    day_after = obj.date_from.astimezone(tz).date() + timedelta(days=1)
    return make_aware(datetime.combine(day_after, time(23, 59, 59)), tz)


def event_end(order: Order):
    """
    The end of the event. For event series, the end of the last date (subevent)
    contained in the order. Without an end date: the end of the day after the start.
    """
    event = order.event
    if event.has_subevents:
        last = (
            event.subevents.filter(orderposition__order=order)
            .order_by("-date_from", "-date_to")
            .first()
        )
        if last:
            return _end_of(last, event.timezone)
    return _end_of(event, event.timezone)


def _set_valid_if_pending(order: Order, value: bool, user=None, auth=None):
    if order.valid_if_pending == value:
        return
    order.valid_if_pending = value
    order.save(update_fields=["valid_if_pending"])
    order.log_action(
        "pretix.event.order.valid_if_pending." + ("set" if value else "unset"),
        user=user,
        auth=auth,
    )


def _set_expires(order: Order, expires, user=None, auth=None):
    order.expires = expires
    order.save(update_fields=["expires"])
    order.log_action(
        "pretix.event.order.expirychanged",
        user=user,
        auth=auth,
        data={"expires": order.expires},
    )


def confirm_unpaid(order: Order, user=None, auth=None):
    """
    Mark the order as "valid if pending" (shown as "Confirmed"; tickets and check-in
    work) and extend the payment deadline until the end of the event. The order and
    the payment stay pending. The deadline is never shortened by this.
    """
    _set_valid_if_pending(order, True, user=user, auth=auth)
    end = event_end(order)
    if end and (not order.expires or end > order.expires):
        _set_expires(order, end, user=user, auth=auth)


def unconfirm(order: Order, user=None, auth=None):
    """
    Undo :func:`confirm_unpaid` when the order no longer uses this payment method:
    remove "valid if pending" and give the order pretix' regular payment term,
    counted from now (as if it had been placed with the new payment method today).
    The deadline is never extended by this.
    """
    if order.status != Order.STATUS_PENDING:
        return
    _set_valid_if_pending(order, False, user=user, auth=auth)
    subevents = None
    if order.event.has_subevents:
        subevents = order.event.subevents.filter(orderposition__order=order).distinct()
    regular = compute_payment_deadline(
        event=order.event,
        sales_channel=order.sales_channel,
        now_dt=now(),
        subevents=subevents,
    )
    if not order.expires or regular < order.expires:
        _set_expires(order, regular, user=user, auth=auth)


# Only characters that can start inline formatting, links or HTML. Escaping more
# would show up as backslashes in the plain text version of the email.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>])")


def _escape_markdown(value):
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


class ExternalVoucherPayment(ManualPayment):
    """
    Works like pretix' manual payment, but asks the customer a question (e.g. for
    an external voucher code), optionally checks the answer's format, records the
    answer on the payment, marks the order as "valid if pending" and extends the
    payment deadline until the end of the event. The payment stays pending until it
    is confirmed manually.
    """

    identifier = IDENTIFIER
    verbose_name = _("External voucher")

    @property
    def public_name(self):
        return str(
            self.settings.get("public_name", as_type=LazyI18nString)
            or _("External voucher")
        )

    @property
    def question(self):
        return str(
            self.settings.get("question", as_type=LazyI18nString) or _("Voucher code")
        )

    @property
    def answer_required(self):
        return self.settings.get("answer_required", True, as_type=bool)

    @property
    def answer_regex(self):
        return self.settings.get("answer_regex", DEFAULT_ANSWER_REGEX, as_type=str)

    def validate_answer(self, value):
        pattern = self.answer_regex
        if not value or not pattern:
            return
        try:
            regex = _compile_full(pattern)
        except re.error:
            return  # invalid pattern, already rejected when saving the settings
        if not regex.match(value):
            raise ValidationError(
                str(
                    self.settings.get("answer_regex_error", as_type=LazyI18nString)
                    or _("Your answer does not have the expected format.")
                )
            )

    # ManualPayment ties these to the built-in manual payment plugin. We bind them
    # to our own plugin instead and never act as an implicit provider.
    def is_implicit(self, request: HttpRequest):
        return False

    def is_allowed(self, request: HttpRequest, total: Decimal = None):
        return "pretix_external_vouchers" in self.event.plugins and (
            BasePaymentProvider.is_allowed(self, request, total)
        )

    def order_change_allowed(self, order: Order, request=None):
        return "pretix_external_vouchers" in self.event.plugins and (
            BasePaymentProvider.order_change_allowed(self, order, request)
        )

    @property
    def settings_form_fields(self):
        d = super().settings_form_fields
        d["_prevent_reminder_mail"].initial = True
        # pretix' ManualPayment copied this label from the bank transfer provider.
        d["invoice_immediately"].label = _(
            "Create an invoice for orders using this payment method immediately if "
            "the event is otherwise configured to create invoices after payment is "
            "completed."
        )
        d["question"] = I18nFormField(
            label=_("Question asked during checkout"),
            help_text=_(
                "For example: 'Please enter your voucher code'. The answer will be "
                "stored with the payment."
            ),
            widget=I18nTextInput,
            required=False,
        )
        d["question_help"] = I18nFormField(
            label=_("Help text for the question"),
            widget=I18nTextInput,
            required=False,
        )
        d["answer_required"] = forms.BooleanField(
            label=_("Answer is required"),
            required=False,
            initial=True,
        )
        d["answer_regex"] = forms.CharField(
            label=_("Required answer format (regular expression)"),
            help_text=_(
                "The whole answer has to match this regular expression. Leave empty "
                "to accept any answer. Default: %(default)s"
            )
            % {"default": DEFAULT_ANSWER_REGEX},
            required=False,
            initial=DEFAULT_ANSWER_REGEX,
            validators=[validate_regex],
        )
        d["answer_regex_error"] = I18nFormField(
            label=_("Error message if the answer has the wrong format"),
            help_text=_(
                "For example: 'Please enter the code in the form XXX-XXX-XXX.'"
            ),
            widget=I18nTextInput,
            required=False,
        )
        return d

    @property
    def payment_form_fields(self):
        return OrderedDict(
            [
                (
                    "answer",
                    forms.CharField(
                        label=self.question,
                        help_text=str(
                            self.settings.get("question_help", as_type=LazyI18nString)
                            or ""
                        ),
                        required=self.answer_required,
                        max_length=255,
                        validators=[self.validate_answer],
                    ),
                ),
            ]
        )

    def payment_form_render(self, request, total=None, order=None) -> str:
        return super().payment_form_render(
            request
        ) + BasePaymentProvider.payment_form_render(self, request, total, order)

    def checkout_prepare(self, request, cart):
        return BasePaymentProvider.checkout_prepare(self, request, cart)

    def payment_is_valid_session(self, request):
        return not self.answer_required or bool(request.session.get(SESSION_KEY))

    def checkout_confirm_render(self, request, order=None, info_data=None):
        # ManualPayment.payment_form_render only renders the checkout description
        return ManualPayment.payment_form_render(self, request) + format_html(
            "<p><strong>{}</strong>: {}</p>",
            self.question,
            request.session.get(SESSION_KEY, "") or "–",
        )

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        # The payment stays pending, it is confirmed manually by the organizer.
        info = payment.info_data
        if request is not None and request.session.get(SESSION_KEY):
            # e.g. a change of the payment method of an existing order
            info["answer"] = request.session[SESSION_KEY]
        elif "answer" not in info:
            info["answer"] = (payment.order.meta_info_data or {}).get(META_KEY, "")
        info.setdefault("question", self.question)
        payment.info_data = info
        payment.save(update_fields=["info"])
        confirm_unpaid(payment.order)
        return None

    def cancel_payment(self, payment: OrderPayment):
        # Called by pretix e.g. when the customer or an admin switches the order to a
        # different payment method. Then neither "valid if pending" nor the extended
        # deadline apply any more.
        super().cancel_payment(payment)
        order = payment.order
        still_ours = (
            order.payments.filter(
                provider=IDENTIFIER,
                state__in=(
                    OrderPayment.PAYMENT_STATE_CREATED,
                    OrderPayment.PAYMENT_STATE_PENDING,
                ),
            )
            .exclude(pk=payment.pk)
            .exists()
        )
        if not still_ours:
            unconfirm(order)

    def _question_and_answer(self, payment: OrderPayment):
        info = payment.info_data
        if "answer" not in info and payment.pk:
            # The instance might predate our order_placed receiver writing the answer
            payment.refresh_from_db(fields=["info"])
            info = payment.info_data
        return info.get("question") or self.question, info.get("answer") or ""

    def payment_pending_render(self, request: HttpRequest, payment: OrderPayment):
        question, answer = self._question_and_answer(payment)
        html = super().payment_pending_render(request, payment)
        if answer:
            html += format_html("<p><strong>{}</strong>: {}</p>", question, answer)
        return html

    def prevent_reminder_mail(self, order, payment) -> bool:
        # No "please pay" reminders by default, the customer does not need to act.
        return self.settings.get("_prevent_reminder_mail", as_type=bool, default=True)

    def order_pending_mail_render(self, order, payment) -> str:
        question, answer = self._question_and_answer(payment)
        text = str(super().order_pending_mail_render(order, payment) or "")
        if answer:
            line = f"{_escape_markdown(question)}: {_escape_markdown(answer)}"
            text = f"{text}\n\n{line}" if text.strip() else line
        return text

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment):
        info = payment.info_data
        return format_html(
            "<dl class='dl-horizontal'><dt>{}</dt><dd>{}</dd></dl>",
            info.get("question") or self.question,
            info.get("answer") or "–",
        )

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        return payment.info_data.get("answer", "")

    def api_payment_details(self, payment: OrderPayment):
        return {
            "question": payment.info_data.get("question"),
            "answer": payment.info_data.get("answer"),
        }

    def matching_id(self, payment: OrderPayment):
        return payment.info_data.get("answer") or None
