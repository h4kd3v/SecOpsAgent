import type { AppConfig, Session } from "../types";
import { IconClose } from "./Icons";

interface Props {
  config: AppConfig | null;
  session: Session;
  onClose: () => void;
}

/**
 * Read-only on purpose. Everything here is decided by the backend `.env` and
 * shared by all analysts; letting one of them change the model or switch off
 * the write-approval gate from the browser would be a per-user override of a
 * deployment-wide security control.
 */
export function SettingsPanel({ config, session, onClose }: Props) {
  return (
    <div className="panel-backdrop" onClick={onClose}>
      <div className="panel panel-narrow" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>Settings</h2>
          <button className="panel-close" onClick={onClose} aria-label="Close">
            <IconClose size={18} />
          </button>
        </div>

        <dl className="settings-list">
          <div>
            <dt>Model</dt>
            <dd>{config?.model_display_name || "—"}</dd>
          </div>
          <div>
            <dt>Approval for write tools</dt>
            <dd>
              {config?.require_approval_for_write ? (
                <span className="badge badge-read">required</span>
              ) : (
                <span className="badge badge-write">not required</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Mode</dt>
            <dd>{config?.demo_mode ? "Demo — no live SecOps calls" : "Live"}</dd>
          </div>
          <div>
            <dt>Session</dt>
            <dd>{session.label}</dd>
          </div>
        </dl>

        <p className="panel-dim">
          These are set by the deployment, not per analyst. History is tied to this
          browser — clearing cookies starts a new session with an empty sidebar.
        </p>
      </div>
    </div>
  );
}
