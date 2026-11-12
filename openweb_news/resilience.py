"""Explicit opt-in automatic recovery; original packages keep their policy."""
PROFILE = "automatic-transient-recovery-v1"
POLICY = dict(profile=PROFILE, uncertain_resends_per_task=1,
              duplicate_charge_risk_accepted=True, transient_pause_action="cooldown")


def enabled(config):
    value = config.get("automatic_recovery")
    if value is not None and value != POLICY:
        raise ValueError("automatic recovery requires the complete declared policy")
    return value is not None
