import type { RecordingConsent } from "@/lib/types";

type RecordingConsentControlProps = {
  consent: RecordingConsent;
  onChanged: () => Promise<unknown>;
  compact?: boolean;
  autoOpen?: boolean;
};

/**
 * Compatibility shim for archived room projections that may still contain a
 * recording_consent field. New matches retain speech text only, so the retired
 * recording policy must never block readiness or appear in the competition UI.
 */
export function RecordingConsentControl(_props: RecordingConsentControlProps) {
  return null;
}
