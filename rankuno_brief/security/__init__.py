"""Security layer: everything that reaches an inbox passes through here.

1. Content screen  (content.py)        every story is checked before it can be selected
2. Output gate     (content.py)        the finished email is checked after build and before sending
3. Recipients      (recipients.py)     only valid, allowed, reachable, non-suppressed addresses
4. Deliverability  (deliverability.py) spam signals in the message; SPF / DKIM / DMARC of the sender

preflight.py runs checks 2-4 (plus a tamper check on the built files) before every send.
"""
