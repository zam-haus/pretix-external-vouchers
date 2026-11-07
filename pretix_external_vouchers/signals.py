from django.dispatch import receiver
from django.templatetags.static import static
from django.utils.html import format_html
from pretix.base.signals import (
    order_placed,
    order_valid_if_pending,
    register_payment_providers,
)
from pretix.presale.signals import html_head, order_meta_from_request

from .payment import (
    IDENTIFIER,
    META_KEY,
    SESSION_KEY,
    ExternalVoucherPayment,
    apply_deadline_and_validity,
)


@receiver(register_payment_providers, dispatch_uid="payment_external_voucher")
def register_payment_provider(sender, **kwargs):
    return ExternalVoucherPayment


@receiver(order_meta_from_request, dispatch_uid="external_voucher_order_meta")
def order_meta(sender, request, **kwargs):
    # During checkout, execute_payment() is called without a request and the cart's
    # payment info_data is not ours to fill, so we carry the answer over via the
    # order's meta_info.
    from pretix.presale.views.cart import cart_session

    payments = cart_session(request).get("payments", [])
    if any(p.get("provider") == IDENTIFIER for p in payments):
        return {META_KEY: request.session.get(SESSION_KEY, "")}
    return {}


@receiver(order_valid_if_pending, dispatch_uid="external_voucher_valid_if_pending")
def valid_if_pending(sender, payments, **kwargs):
    return any(p.get("provider") == IDENTIFIER for p in payments or [])


@receiver(order_placed, dispatch_uid="external_voucher_order_placed")
def order_placed_receiver(sender, order, **kwargs):
    # Runs before the order confirmation mail is sent, so the mail already shows
    # the extended payment deadline.
    payments = [p for p in order.payments.all() if p.provider == IDENTIFIER]
    if not payments:
        return
    provider = sender.get_payment_providers(cached=True).get(IDENTIFIER)
    answer = (order.meta_info_data or {}).get(META_KEY, "")
    for p in payments:
        info = p.info_data
        info.setdefault("answer", answer)
        if provider:
            info.setdefault("question", provider.question)
        p.info_data = info
        p.save(update_fields=["info"])
    apply_deadline_and_validity(order)


@receiver(html_head, dispatch_uid="external_voucher_html_head")
def hide_voucher_redemption(sender, request=None, **kwargs):
    # Only hides the "Redeem a voucher" box and cart toggle. Voucher links
    # (/redeem?voucher=...) keep working.
    return format_html(
        '<link rel="stylesheet" type="text/css" href="{}">',
        static("pretix_external_vouchers/hide_vouchers.css"),
    )
