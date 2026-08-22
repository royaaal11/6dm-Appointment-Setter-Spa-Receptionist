import { useEffect, useState } from "react";
import { Lock, Phone, Save } from "lucide-react";
import {
  fetchMySpaAccount,
  fetchSpaAccount,
  updateSpaAccount,
  type BookingProvider,
  type SpaAccount,
} from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { canEditSpaSettings } from "../../auth/roles";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";

const PROVIDERS: BookingProvider[] = [
  "google_calendar",
  "mindbody",
  "mangomint",
  "square",
  "vagaro",
  "zenoti",
];

const DAYS: [string, string][] = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
];

/**
 * The receptionist configuration for one spa — the whole of what onboarding
 * touches. Everything here is a column on the tenant's `SpaAccount` row, read
 * by the inbound webhook at call time, so editing it changes how the spa's
 * phone is answered without a deploy.
 */
export default function SpaSettings() {
  const { role, effectiveTenantId, impersonatedTenantId } = useAuth();
  const editable = canEditSpaSettings(role);

  const [spa, setSpa] = useState<SpaAccount | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setLoading(true);
    // A super admin reads the spa they picked in the switcher; a spa user's own
    // tenant comes from their token, with no id to supply or tamper with.
    const request = impersonatedTenantId
      ? fetchSpaAccount(impersonatedTenantId)
      : fetchMySpaAccount();
    request
      .then((account) => {
        setSpa(account);
        setError(null);
      })
      .catch(() =>
        setError(
          "No spa account is attached to this view. Pick one from the tenant switcher."
        )
      )
      .finally(() => setLoading(false));
  }, [impersonatedTenantId, effectiveTenantId]);

  const patch = <K extends keyof SpaAccount>(key: K, value: SpaAccount[K]) => {
    setSpa((current) => (current ? { ...current, [key]: value } : current));
    setSaved(false);
  };

  const setHours = (day: string, field: "open" | "close", value: string) => {
    if (!spa) return;
    const existing = spa.business_hours[day]?.[0] ?? { open: "", close: "" };
    const next = { ...spa.business_hours };
    if (!value && field === "open") delete next[day];
    else next[day] = [{ ...existing, [field]: value }];
    patch("business_hours", next);
  };

  const save = async () => {
    if (!spa) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateSpaAccount(spa.id, {
        name: spa.name,
        grok_system_prompt: spa.grok_system_prompt,
        business_hours: spa.business_hours,
        services: spa.services,
        staff: spa.staff,
        timezone: spa.timezone,
        booking_provider: spa.booking_provider,
        twiml_voice: spa.twiml_voice,
      });
      setSpa(updated);
      setSaved(true);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      setError(detail || "Unable to save settings.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <PageHeader
        eyebrow="Spa Receptionist"
        title="Receptionist settings"
        subtitle="How your AI answers the phone: persona, service menu, team and opening hours."
        actions={
          editable && spa ? (
            <button
              onClick={save}
              disabled={saving}
              className="inline-flex items-center gap-2 rounded-xl bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 transition hover:bg-cyan-300 disabled:opacity-60"
            >
              <Save size={15} /> {saving ? "Saving…" : "Save changes"}
            </button>
          ) : undefined
        }
      />

      {!editable && (
        <p className="flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-800/40 px-3 py-2.5 text-[11px] text-slate-400">
          <Lock size={13} /> Your account has read-only access to these settings.
        </p>
      )}
      {saved && (
        <p className="rounded-xl border border-emerald-400/20 bg-emerald-400/5 px-3 py-2.5 text-[11px] text-emerald-300">
          Saved. New calls will use these settings immediately.
        </p>
      )}

      <StateBlock loading={loading} error={error}>
        {spa && (
          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="Identity">
              <label className="block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Spa name
                </span>
                <input
                  value={spa.name}
                  disabled={!editable}
                  onChange={(event) => patch("name", event.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>

              <div className="mt-4">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Inbound number
                </span>
                <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-[#07111f] px-3 py-2.5">
                  <Phone size={14} className="text-slate-500" />
                  <span className="text-sm text-slate-300">
                    {spa.twilio_phone_number || "not assigned"}
                  </span>
                  <Lock size={12} className="ml-auto text-slate-600" />
                </div>
                <p className="mt-1.5 text-[10px] text-slate-500">
                  Assigned by 6DM. Calls to this number are routed to your
                  receptionist.
                </p>
              </div>

              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Timezone
                </span>
                <input
                  value={spa.timezone}
                  disabled={!editable}
                  onChange={(event) => patch("timezone", event.target.value)}
                  placeholder="America/Los_Angeles"
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
                <span className="mt-1.5 block text-[10px] text-slate-500">
                  Opening hours below are interpreted in this timezone.
                </span>
              </label>

              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Booking system
                </span>
                <select
                  value={spa.booking_provider}
                  disabled={!editable}
                  onChange={(event) =>
                    patch("booking_provider", event.target.value as BookingProvider)
                  }
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-slate-200 outline-none disabled:opacity-60"
                >
                  {PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
                {!spa.booking_provider_configured && (
                  <span className="mt-1.5 block text-[10px] text-amber-300">
                    Credentials for this provider are not in place yet. Bookings are
                    held on your calendar until 6DM connects it — nothing is lost.
                  </span>
                )}
              </label>
            </Panel>

            <Panel
              title="Receptionist persona"
              subtitle="Prepended to the shared voice rules for every call."
            >
              <textarea
                rows={10}
                value={spa.grok_system_prompt ?? ""}
                disabled={!editable}
                onChange={(event) => patch("grok_system_prompt", event.target.value)}
                placeholder="Greet guests warmly, mention the tea bar, and always confirm the therapist by name."
                className="w-full resize-y rounded-lg border border-slate-700 bg-[#07111f] px-3 py-3 font-mono text-xs leading-relaxed text-slate-200 outline-none disabled:opacity-60"
              />
            </Panel>

            <Panel title="Opening hours" subtitle="Bookings outside these are refused.">
              <div className="space-y-2">
                {DAYS.map(([key, label]) => {
                  const window = spa.business_hours[key]?.[0];
                  return (
                    <div key={key} className="flex items-center gap-2">
                      <span className="w-20 text-[11px] text-slate-400">{label}</span>
                      <input
                        type="time"
                        value={window?.open ?? ""}
                        disabled={!editable}
                        onChange={(event) => setHours(key, "open", event.target.value)}
                        className="rounded-lg border border-slate-700 bg-[#07111f] px-2 py-1.5 text-xs text-slate-200 outline-none disabled:opacity-60"
                      />
                      <span className="text-[11px] text-slate-600">to</span>
                      <input
                        type="time"
                        value={window?.close ?? ""}
                        disabled={!editable}
                        onChange={(event) => setHours(key, "close", event.target.value)}
                        className="rounded-lg border border-slate-700 bg-[#07111f] px-2 py-1.5 text-xs text-slate-200 outline-none disabled:opacity-60"
                      />
                      {!window && (
                        <span className="text-[10px] text-slate-600">closed</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </Panel>

            <Panel title="Service menu" subtitle="Names and durations the agent quotes.">
              <div className="space-y-2">
                {spa.services.map((service, index) => (
                  <div key={index} className="flex items-center gap-2">
                    <input
                      value={service.name}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "services",
                          spa.services.map((item, i) =>
                            i === index ? { ...item, name: event.target.value } : item
                          )
                        )
                      }
                      className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                    <input
                      type="number"
                      min={5}
                      value={service.duration_minutes}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "services",
                          spa.services.map((item, i) =>
                            i === index
                              ? { ...item, duration_minutes: Number(event.target.value) }
                              : item
                          )
                        )
                      }
                      className="w-20 rounded-lg border border-slate-700 bg-[#07111f] px-2 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                    <span className="text-[10px] text-slate-600">min</span>
                  </div>
                ))}
                {editable && (
                  <button
                    onClick={() =>
                      patch("services", [
                        ...spa.services,
                        { name: "", duration_minutes: 60 },
                      ])
                    }
                    className="mt-2 w-full rounded-lg border border-slate-700 py-2 text-[11px] font-semibold text-slate-400 hover:border-cyan-400/40 hover:text-cyan-300"
                  >
                    Add service
                  </button>
                )}
                {spa.services.length === 0 && !editable && (
                  <p className="text-[11px] text-slate-500">No services configured.</p>
                )}
              </div>
            </Panel>

            <Panel
              title="Team"
              subtitle="Also sets how many appointments can run at once."
            >
              <div className="space-y-2">
                {spa.staff.map((member, index) => (
                  <div key={index} className="flex items-center gap-2">
                    <input
                      value={member.name}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "staff",
                          spa.staff.map((item, i) =>
                            i === index ? { ...item, name: event.target.value } : item
                          )
                        )
                      }
                      placeholder="Name"
                      className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                    <input
                      value={member.role ?? ""}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "staff",
                          spa.staff.map((item, i) =>
                            i === index ? { ...item, role: event.target.value } : item
                          )
                        )
                      }
                      placeholder="Role"
                      className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                  </div>
                ))}
                {editable && (
                  <button
                    onClick={() =>
                      patch("staff", [...spa.staff, { name: "", role: "", services: [] }])
                    }
                    className="mt-2 w-full rounded-lg border border-slate-700 py-2 text-[11px] font-semibold text-slate-400 hover:border-cyan-400/40 hover:text-cyan-300"
                  >
                    Add team member
                  </button>
                )}
              </div>
            </Panel>
          </div>
        )}
      </StateBlock>
    </>
  );
}
