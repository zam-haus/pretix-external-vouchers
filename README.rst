External Vouchers
=================

This is a plugin for `pretix`_ that lets customers pay with vouchers managed outside of pretix.

Once the plugin is enabled for an event, it does the following:

* It adds a payment method **External voucher**, based on pretix' manual payment. During checkout it asks the
  customer a configurable question (e.g. "Please enter your voucher code") and stores the answer on the payment
  (visible in the backend order view, in the REST API as ``details.answer`` and searchable as matching ID).
* The answer can be checked against a regular expression (default ``[0-9a-zA-Z]{3}-[0-9a-zA-Z]{3}-[0-9a-zA-Z]{3}``,
  the whole answer has to match; leave the setting empty to accept any answer). The error message is configurable.
* Orders using this payment method are marked as *valid if pending*: customers see the status "Confirmed", tickets
  can be downloaded and checked in. The order and the payment stay pending (unpaid) until the payment is confirmed
  manually. pretix' order page still shows its standard "A payment of ... is still pending" panel, followed by the
  payment method's "pending description" and the customer's answer.
* The payment deadline is extended until the end of the event (for event series: the end of the last date in the
  order). If no end is set, the deadline is the end of the day after the start.
* If the order is switched to another payment method (by the customer or in the backend), or the payment is
  canceled, *valid if pending* is removed and the deadline goes back to the event's regular payment term, counted
  from that moment.
* Orders that require approval are not touched until they are approved and paid with this method.
* The answer is shown to the customer on the order page and in the order confirmation email (``{payment_info}``).
* Payment reminder emails are off by default for this payment method (setting "Do not send a payment reminder mail").
* It hides pretix' built-in "Redeem a voucher" (Gutschein einlösen) box on the event page, in the cart and in the
  embeddable widget. Built-in vouchers keep working through voucher links (``/redeem?voucher=CODE``) and the
  widget's ``data-voucher`` attribute.

Configure the payment method under *Event settings → Payment → External voucher*.

Development setup
-----------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for pretix development.

4. Execute ``pip install -e .`` within this directory to register this application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by enabling it in
   the 'plugins' tab in the settings.

This plugin has CI set up to enforce a few code style rules. To check locally, you need these packages installed::

    pip install flake8 isort black

To check your plugin for rule violations, run::

    black --check .
    isort -c .
    flake8 .

You can auto-fix some of these issues by running::

    isort .
    black .

To automatically check for these issues before you commit, you can run ``.install-hooks``.


License
-------


Copyright 2026 Julian Hammer

Released under the terms of the Apache License 2.0



.. _pretix: https://github.com/pretix/pretix
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
