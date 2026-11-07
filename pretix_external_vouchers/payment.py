from collections import OrderedDict
from decimal import Decimal
from django import forms
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import BasePaymentProvider, ManualPayment

IDENTIFIER = "external_voucher"
SESSION_KEY = f"payment_{IDENTIFIER}_answer"
META_KEY = "external_voucher_answer"


def event_end(order: Order):
    """
    The end of the event. For event series, the end of the last date (subevent)
    contained in the order. Falls back to the start if no end is configured.
    """
    event = order.event
    if event.has_subevents:
        last = (
            event.subevents.filter(orderposition__order=order)
            .order_by("-date_from", "-date_to")
            .first()
        )
        if last:
            return last.date_to or last.date_from
    return event.date_to or event.date_from


def apply_deadline_and_validity(order: Order, user=None, auth=None):
    """
    Extend the payment deadline until the end of the event and treat the order as
    valid even though it has not been paid yet.
    """
    update_fields = []
    end = event_end(order)
    if end and (not order.expires or end > order.expires):
        order.expires = end
        update_fields.append("expires")
    if not order.valid_if_pending:
        order.valid_if_pending = True
        update_fields.append("valid_if_pending")
    if not update_fields:
        return

    order.save(update_fields=update_fields)
    if "valid_if_pending" in update_fields:
        order.log_action(
            "pretix.event.order.valid_if_pending.set", user=user, auth=auth
        )
    if "expires" in update_fields:
        order.log_action(
            "pretix.event.order.expirychanged",
            user=user,
            auth=auth,
            data={"expires": order.expires},
        )


class ExternalVoucherPayment(ManualPayment):
    """
    Works like pretix' manual payment, but asks the customer a question (e.g. for
    an external voucher code), records the answer on the payment, extends the
    payment deadline until the end of the event and marks the order as valid
    before the payment is actually confirmed.
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
        apply_deadline_and_validity(payment.order)
        return None

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
