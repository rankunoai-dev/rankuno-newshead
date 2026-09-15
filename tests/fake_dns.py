from rankuno_brief.security.dns import DnsError, DnsResolver


class _NoClient:
    def close(self):
        pass


class FakeDns(DnsResolver):
    """Answers from a dict instead of the network: {("rankuno.com", "TXT"): ['"v=spf1 -all"']}."""

    def __init__(self, records=None, *, offline=False):
        super().__init__(client=_NoClient())
        self.records = records or {}
        self.offline = offline
        self.queries = []

    def _query(self, name, record_type):
        self.queries.append((name, record_type))
        if self.offline:
            raise DnsError(f"DNS lookup for {name} ({record_type}) failed: offline")
        return list(self.records.get((name, record_type), []))
