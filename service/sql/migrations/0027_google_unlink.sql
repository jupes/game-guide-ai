-- Allow the existing content-free identity ledger to record an unlink.
ALTER TABLE auth.identity_events
  DROP CONSTRAINT identity_events_action_check,
  ADD CONSTRAINT identity_events_action_check
    CHECK (action IN ('sign_in', 'sign_up', 'link', 'unlink'));
