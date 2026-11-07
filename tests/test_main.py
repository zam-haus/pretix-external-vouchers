import datetime
import pytest
from decimal import Decimal
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import CartPosition, Event, Item, Order, Organizer, Quota
from pretix.base.services.orders import _perform_order

from pretix_external_vouchers.payment import (
    IDENTIFIER,
    META_KEY,
    ExternalVoucherPayment,
    validate_regex,
)
from pretix_external_vouchers.signals import hide_voucher_redemption

PLUGINS = "pretix.plugins.manualpayment,pretix_external_vouchers"


@pytest.fixture
def event():
    with scopes_disabled():
        orga = Organizer.objects.create(name="ZAM", slug="zam")
        start = now().replace(microsecond=0) + datetime.timedelta(days=60)
        ev = Event.objects.create(
            organizer=orga,
            name="Event",
            slug="event",
            date_from=start,
            date_to=start + datetime.timedelta(days=2),
            live=True,
            plugins=PLUGINS,
        )
        ev.settings.set(f"payment_{IDENTIFIER}__enabled", True)
        ev.settings.set(f"payment_{IDENTIFIER}_question", "Voucher number?")
        yield ev


@pytest.fixture
def item(event):
    with scopes_disabled():
        item = Item.objects.create(event=event, name="Ticket", default_price=23)
        quota = Quota.objects.create(event=event, name="All", size=None)
        quota.items.add(item)
        return item


def _payment(provider):
    return [
        {
            "id": "test1",
            "provider": provider,
            "max_value": None,
            "min_value": None,
            "multi_use_supported": False,
            "info_data": {},
        }
    ]


def _order(event, item, provider, meta=None, subevents=(None,)):
    cps = [
        CartPosition.objects.create(
            event=event,
            item=item,
            price=Decimal("23.00"),
            expires=now() + datetime.timedelta(minutes=10),
            cart_id="abc",
            subevent=se,
        )
        for se in subevents
    ]
    _perform_order(
        event,
        _payment(provider),
        [cp.pk for cp in cps],
        "admin@example.org",
        "en",
        None,
        meta or {},
        "web",
    )
    return Order.objects.get()


@pytest.mark.django_db
def test_provider_is_registered_and_not_implicit(event):
    prov = event.get_payment_providers()[IDENTIFIER]
    assert isinstance(prov, ExternalVoucherPayment)
    request = RequestFactory().get("/")
    assert not prov.is_implicit(request)
    assert prov.question == "Voucher number?"
    assert prov.public_name == "External voucher"


@pytest.mark.django_db
def test_order_with_external_voucher(event, item):
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "ABC-123"})
        assert order.status == Order.STATUS_PENDING
        assert order.valid_if_pending
        assert order.expires == event.date_to
        payment = order.payments.get()
        assert payment.provider == IDENTIFIER
        assert payment.state == payment.PAYMENT_STATE_CREATED
        assert payment.info_data["answer"] == "ABC-123"
        assert payment.info_data["question"] == "Voucher number?"
        assert payment.payment_provider.matching_id(payment) == "ABC-123"


@pytest.mark.django_db
def test_order_with_other_provider_untouched(event, item):
    with scopes_disabled():
        order = _order(event, item, "manual")
        assert not order.valid_if_pending
        assert order.expires < event.date_from


def _end_of_next_day(dt, event):
    tz = event.timezone
    day = dt.astimezone(tz).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(day, datetime.time(23, 59, 59), tzinfo=tz)


@pytest.mark.django_db
@pytest.mark.parametrize("tz", ["UTC", "Europe/Berlin"])
def test_event_without_end_uses_day_after_start(event, item, tz):
    with scopes_disabled():
        event.settings.timezone = tz
        event.date_to = None
        event.save()
        event = Event.objects.get(pk=event.pk)
        order = _order(event, item, IDENTIFIER, {META_KEY: "X"})
        assert order.expires == _end_of_next_day(event.date_from, event)
        assert order.expires > event.date_from


def _series(event, item, *dates):
    event.has_subevents = True
    event.save()
    event.quotas.all().delete()
    subevents = []
    for date_from, date_to in dates:
        se = event.subevents.create(
            name="x", date_from=date_from, date_to=date_to, active=True
        )
        event.quotas.create(name="x", size=None, subevent=se).items.add(item)
        subevents.append(se)
    return subevents


@pytest.mark.django_db
def test_event_end_series_uses_last_subevent_in_order(event, item):
    d = event.date_from
    with scopes_disabled():
        # se1 starts first but ends last; se2 is the last date in the order;
        # se3 is later but not part of the order.
        se1, se2, se3 = _series(
            event,
            item,
            (d, d + datetime.timedelta(days=30)),
            (d + datetime.timedelta(days=10), d + datetime.timedelta(days=11)),
            (d + datetime.timedelta(days=50), d + datetime.timedelta(days=51)),
        )
        order = _order(event, item, IDENTIFIER, {META_KEY: "X"}, subevents=[se2, se1])
        assert order.positions.count() == 2
        assert order.expires == se2.date_to


@pytest.mark.django_db
def test_event_end_series_without_end_date(event, item):
    d = event.date_from
    with scopes_disabled():
        (se1,) = _series(event, item, (d + datetime.timedelta(days=5), None))
        order = _order(event, item, IDENTIFIER, {META_KEY: "X"}, subevents=[se1])
        assert order.expires == _end_of_next_day(se1.date_from, event)


@pytest.mark.django_db
def test_html_head_hides_voucher_box(event):
    html = hide_voucher_redemption(event, request=RequestFactory().get("/"))
    assert "pretix_external_vouchers/hide_vouchers.css" in html


def _request(event, session=None):
    from django.contrib.sessions.backends.db import SessionStore

    request = RequestFactory().get("/")
    request.event = event
    request.session = SessionStore()
    request.session.update(session or {})
    return request


@pytest.mark.django_db
def test_order_meta_from_request(event, monkeypatch):
    from pretix.presale.views import cart

    from pretix_external_vouchers.payment import SESSION_KEY
    from pretix_external_vouchers.signals import order_meta

    request = _request(event, {SESSION_KEY: "ABC-123"})
    monkeypatch.setattr(
        cart, "cart_session", lambda r: {"payments": _payment(IDENTIFIER)}
    )
    assert order_meta(event, request=request) == {META_KEY: "ABC-123"}
    monkeypatch.setattr(
        cart, "cart_session", lambda r: {"payments": _payment("manual")}
    )
    assert order_meta(event, request=request) == {}


@pytest.mark.django_db
def test_checkout_form_and_session(event):
    from pretix_external_vouchers.payment import SESSION_KEY

    prov = event.get_payment_providers()[IDENTIFIER]
    request = _request(event)
    assert "Voucher number?" in prov.payment_form_render(request, Decimal("23.00"))
    assert not prov.payment_is_valid_session(request)

    request = _request(event, {SESSION_KEY: "ABC-123"})
    assert prov.payment_is_valid_session(request)
    html = prov.checkout_confirm_render(request)
    assert "Voucher number?" in html and "ABC-123" in html

    event.settings.set(f"payment_{IDENTIFIER}_answer_required", False)
    prov = event.get_payment_providers()[IDENTIFIER]
    assert prov.payment_is_valid_session(_request(event))


def _checkout(client, event, item, data):
    from pretix.testutils.sessions import get_cart_session_key

    base = f"/{event.organizer.slug}/{event.slug}"
    client.get(f"{base}/")
    client.post(f"{base}/cart/add", {f"item_{item.pk}": "1"})
    session = client.session
    session["carts"][get_cart_session_key(client, event)]["email"] = "a@example.org"
    session.save()
    r = client.get(f"{base}/checkout/payment/")
    html = r.content.decode()
    client.post(f"{base}/checkout/payment/", data)
    client.post(f"{base}/checkout/confirm/")
    return html


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data,ours",
    [
        ({"payment": IDENTIFIER, f"payment_{IDENTIFIER}-answer": "AB1-c2D-3EF"}, True),
        ({"payment": "manual"}, False),
    ],
)
def test_checkout_side_by_side_with_manual_payment(event, item, client, data, ours):
    event.settings.set("payment_manual__enabled", True)
    event.settings.set("attendee_names_asked", False)
    html = _checkout(client, event, item, data)
    # Both payment methods are offered in the same checkout
    assert f'value="{IDENTIFIER}"' in html
    assert 'value="manual"' in html

    with scopes_disabled():
        order = Order.objects.get()
        payment = order.payments.get()
        assert payment.provider == data["payment"]
        assert order.valid_if_pending is ours
        if ours:
            assert order.expires == event.date_to
            assert payment.info_data["answer"] == "AB1-c2D-3EF"
        else:
            assert order.expires < event.date_from
            assert "answer" not in payment.info_data


@pytest.mark.django_db
@pytest.mark.parametrize(
    "answer,ok",
    [
        ("AB1-c2D-3EF", True),
        ("ABC-123", False),
        ("ABC-123-XYZ-000", False),
        (" ABC-123-XYZ", False),
        ("ABC_123_XYZ", False),
        ("ÄBC-123-XYZ", False),
    ],
)
def test_answer_format_default(event, answer, ok):
    from django.core.exceptions import ValidationError

    prov = event.get_payment_providers()[IDENTIFIER]
    if ok:
        prov.validate_answer(answer)
    else:
        with pytest.raises(ValidationError):
            prov.validate_answer(answer)


@pytest.mark.django_db
def test_answer_format_configurable(event):
    from django.core.exceptions import ValidationError

    event.settings.set(f"payment_{IDENTIFIER}_answer_regex", "")
    event.settings.set(f"payment_{IDENTIFIER}_answer_regex_error", "Use XXX-XXX-XXX")
    prov = event.get_payment_providers()[IDENTIFIER]
    prov.validate_answer("anything goes")

    event.settings.set(f"payment_{IDENTIFIER}_answer_regex", r"\d{4}")
    prov = event.get_payment_providers()[IDENTIFIER]
    prov.validate_answer("1234")
    with pytest.raises(ValidationError, match="Use XXX-XXX-XXX"):
        prov.validate_answer("12345")


def test_invalid_regex_is_rejected_in_settings():
    from django.core.exceptions import ValidationError

    validate_regex("")
    validate_regex(r"[0-9]{3}")
    with pytest.raises(ValidationError):
        validate_regex("[0-9")


@pytest.mark.django_db
def test_settings_form(event):
    prov = event.get_payment_providers()[IDENTIFIER]
    fields = prov.settings_form_fields
    assert "bank transfer" not in str(fields["invoice_immediately"].label)
    assert (
        fields["answer_regex"].initial
        == r"[0-9a-zA-Z]{3}-[0-9a-zA-Z]{3}-[0-9a-zA-Z]{3}"
    )


@pytest.mark.django_db
def test_checkout_rejects_wrong_format(event, item, client):
    event.settings.set("attendee_names_asked", False)
    _checkout(
        client,
        event,
        item,
        {"payment": IDENTIFIER, f"payment_{IDENTIFIER}-answer": "ABC-123"},
    )
    with scopes_disabled():
        assert not Order.objects.exists()


@pytest.mark.django_db
def test_switching_to_other_method_unconfirms(event, item):
    from django.db import transaction
    from pretix.base.services.orders import change_payment_provider

    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
        assert order.expires == event.date_to
        assert order.valid_if_pending
        manual = event.get_payment_providers()["manual"]
        with transaction.atomic():
            change_payment_provider(order, manual)
        order.refresh_from_db()
        assert not order.valid_if_pending
        assert order.expires < event.date_from
        assert (
            order.all_logentries()
            .filter(action_type="pretix.event.order.valid_if_pending.unset")
            .exists()
        )
        assert order.payments.get(provider=IDENTIFIER).state == "canceled"
        assert order.payments.filter(provider="manual").exists()


@pytest.mark.django_db
def test_canceling_payment_in_backend_unconfirms(event, item):
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
        payment = order.payments.get()
        payment.payment_provider.cancel_payment(payment)
        order.refresh_from_db()
        assert not order.valid_if_pending
        assert order.expires < event.date_from


@pytest.mark.django_db
def test_order_requiring_approval_is_not_confirmed(event, item):
    with scopes_disabled():
        item.require_approval = True
        item.save()
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
        assert order.require_approval
        assert not order.payments.exists()
        assert not order.valid_if_pending


@pytest.mark.django_db
def test_order_page_shows_confirmed(event, item, client):
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
    r = client.get(f"/zam/event/order/{order.code}/{order.secret}/")
    html = r.content.decode()
    assert "Confirmed" in html
    assert "Payment pending" not in html


@pytest.mark.django_db
def test_answer_shown_to_customer(event, item):
    from django.core import mail

    event.settings.set(f"payment_{IDENTIFIER}_email_instructions", "Bring the voucher.")
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
        payment = order.payments.get()
        html = payment.payment_provider.payment_pending_render(None, payment)
        assert "Voucher number?" in html and "AB1-C2D-3EF" in html

        # order confirmation mail
        assert len(mail.outbox) == 1
        body = mail.outbox[0].body
        assert "Bring the voucher." in body
        assert "Voucher number?: AB1-C2D-3EF" in body
        html_mail = "".join(
            part.get_payload(decode=True).decode()
            for part in mail.outbox[0].alternatives[0][0].walk()
            if part.get_content_type() == "text/html"
        )
        assert "AB1-C2D-3EF" in html_mail
        assert "AB1\\-C2D" not in html_mail


@pytest.mark.django_db
def test_order_page_shows_answer(event, item, client):
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
    r = client.get(f"/zam/event/order/{order.code}/{order.secret}/")
    assert r.status_code == 200
    html = r.content.decode()
    assert "Voucher number?" in html and "AB1-C2D-3EF" in html


@pytest.mark.django_db
def test_no_reminder_mail_by_default(event, item):
    with scopes_disabled():
        order = _order(event, item, IDENTIFIER, {META_KEY: "AB1-C2D-3EF"})
        payment = order.payments.get()
        prov = payment.payment_provider
        assert prov.prevent_reminder_mail(order, payment)
        assert prov.settings_form_fields["_prevent_reminder_mail"].initial is True
        event.settings.set(f"payment_{IDENTIFIER}__prevent_reminder_mail", False)
        prov = event.get_payment_providers()[IDENTIFIER]
        assert not prov.prevent_reminder_mail(order, payment)


@pytest.mark.django_db
def test_widget_hides_voucher_field(event, item, client):
    with scopes_disabled():
        event.vouchers.create(code="ABC")
    r = client.get("/zam/event/widget/product_list")
    assert r.status_code == 200
    assert r.json()["vouchers_exist"] is False

    # Sanity check: without the plugin, pretix would show the field
    event.plugins = "pretix.plugins.manualpayment"
    event.save()
    from django.core.cache import cache

    cache.clear()
    r = client.get("/zam/event/widget/product_list")
    assert r.json()["vouchers_exist"] is True


@pytest.mark.django_db
def test_html_head_includes_script_fallback(event):
    html = hide_voucher_redemption(event, request=RequestFactory().get("/"))
    assert "pretix_external_vouchers/hide_vouchers.js" in html
