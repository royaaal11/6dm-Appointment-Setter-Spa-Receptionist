import axios, { type AxiosInstance } from "axios";

const BASE_URL: string = (import.meta.env.VITE_API_BASE_URL as string) || "http://127.0.0.1:8000";

export const ACCESS_TOKEN_KEY = "access_token";
export const REFRESH_TOKEN_KEY = "refresh_token";
const ACTIVE_TENANT_KEY = "active_tenant_id";

export const apiClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 10000,
  headers: { "Content-Type": "application/json" },
});

// ---------------------------------------------------------------------------
// Tenant impersonation (super admin tenant switcher)
// ---------------------------------------------------------------------------
// Held here rather than threaded through every call site so no request can
// accidentally omit it. The backend ignores this header for spa users and
// rejects it outright if it names a tenant they don't belong to, so it is a
// convenience for 6DM staff, never a way to widen access.
let activeTenantId: string | null = localStorage.getItem(ACTIVE_TENANT_KEY);

export const getActiveTenantId = (): string | null => activeTenantId;

export const setActiveTenantId = (tenantId: string | null): void => {
  activeTenantId = tenantId;
  if (tenantId) localStorage.setItem(ACTIVE_TENANT_KEY, tenantId);
  else localStorage.removeItem(ACTIVE_TENANT_KEY);
};

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY) || localStorage.getItem("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  if (activeTenantId) {
    config.headers["X-Tenant-Id"] = activeTenantId;
  }
  return config;
});

/** Fires when the API rejects our credentials, so AuthContext can sign out. */
export type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export const setUnauthorizedHandler = (handler: UnauthorizedHandler | null): void => {
  onUnauthorized = handler;
};

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      onUnauthorized?.();
    }
    return Promise.reject(error);
  }
);

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export type UserRole = "super_admin" | "spa_admin" | "spa_staff";

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  role: UserRole;
  tenant_id: string | null;
  twilio_phone_number: string | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export type BookingProvider =
  | "google_calendar"
  | "mindbody"
  | "mangomint"
  | "square"
  | "vagaro"
  | "zenoti";

export interface BusinessHoursWindow {
  open: string;
  close: string;
}

export interface SpaService {
  name: string;
  duration_minutes: number;
  price?: string | null;
  description?: string | null;
}

export interface SpaStaffMember {
  name: string;
  role?: string | null;
  services: string[];
}

export interface SpaAccount {
  id: string;
  name: string;
  twilio_phone_number: string | null;
  grok_system_prompt: string | null;
  business_hours: Record<string, BusinessHoursWindow[]>;
  services: SpaService[];
  staff: SpaStaffMember[];
  timezone: string;
  booking_provider: BookingProvider;
  twiml_voice: string | null;
  is_active: boolean;
  booking_provider_configured: boolean;
  created_at: string;
  updated_at: string;
}

export interface SpaAccountSummary {
  id: string;
  name: string;
  twilio_phone_number: string | null;
  booking_provider: BookingProvider;
  is_active: boolean;
}

export interface Contact {
  id: string;
  owner_id: string | null;
  tenant_id: string | null;
  first_name?: string | null;
  last_name?: string | null;
  full_name: string;
  phone_number: string;
  email?: string | null;
  extra_metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type ContactInput = {
  first_name?: string | null;
  last_name?: string | null;
  phone_number: string;
  email?: string | null;
};

export interface Lead extends Contact {
  call_count: number;
  last_call_at: string | null;
  upcoming_appointment_at: string | null;
}

// Mirrors backend CallStatus / CallDirection (app/models/call_log.py) — these are
// snake_case enum *values*, not hyphenated.
export type CallStatus =
  | "queued"
  | "ringing"
  | "in_progress"
  | "completed"
  | "busy"
  | "failed"
  | "no_answer"
  | "cancelled";

export type CallDirection = "inbound" | "outbound";

export interface CallLog {
  id: string;
  user_id: string | null;
  tenant_id: string | null;
  contact_id: string | null;
  contact?: { id: string; full_name: string; phone_number: string } | null;
  twilio_call_sid: string;
  direction: CallDirection;
  status: CallStatus;
  from_number: string;
  to_number: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  transcript: string | null;
  recording_url: string | null;
  ai_summary: string | null;
  ai_analysis: Record<string, unknown>;
  created_at: string;
}

export type AppointmentStatus =
  | "scheduled"
  | "confirmed"
  | "cancelled"
  | "completed"
  | "no_show";

export interface Appointment {
  id: string;
  user_id: string | null;
  tenant_id: string | null;
  contact_id: string;
  source_call_id: string | null;
  title: string;
  description: string | null;
  status: AppointmentStatus;
  start_time: string;
  end_time: string;
  booking_provider: string | null;
  external_booking_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalyticsSummary {
  tenant_id: string | null;
  scope: "spa" | "sales_workspace";
  window_start: string;
  window_end: string;
  calls: {
    total: number;
    inbound: number;
    outbound: number;
    completed: number;
    failed: number;
  };
  bookings: {
    total: number;
    scheduled: number;
    confirmed: number;
    cancelled: number;
    completed: number;
    no_show: number;
  };
  sentiment: {
    positive: number;
    neutral: number;
    negative: number;
    unscored: number;
  };
  average_call_duration_seconds: number | null;
  booking_conversion_rate: number;
  contacts_total: number;
}

// Backend list endpoints return the paginated envelope from
// app/schemas/common.py (Page[T]), not a bare array.
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

/** The number on the other end of the call, whichever direction it went. */
export const counterpartyNumber = (log: CallLog): string =>
  log.direction === "outbound" ? log.to_number : log.from_number;

export interface OutboundCallResponse {
  call_sid: string;
  call_log_id: string;
  status: string;
}

// NOTE: no trailing slashes below. The routers register their list/create routes
// as `@router.get("")`, so "/api/v1/contacts/" triggers a 307 redirect — an extra
// round trip that also forces a second CORS preflight in the browser.

// --- Auth ---
export const login = async (email: string, password: string): Promise<TokenPair> => {
  const { data } = await apiClient.post<TokenPair>("/api/v1/auth/login", { email, password });
  return data;
};

export const logout = async (refreshToken: string): Promise<void> => {
  await apiClient.post("/api/v1/auth/logout", { refresh_token: refreshToken });
};

export const fetchMe = async (): Promise<CurrentUser> => {
  const { data } = await apiClient.get<CurrentUser>("/api/v1/auth/me");
  return data;
};

// --- Spa accounts ---
export const fetchSpaAccounts = async (): Promise<SpaAccountSummary[]> => {
  const { data } = await apiClient.get<Page<SpaAccountSummary>>("/api/v1/spa-accounts");
  return data.items;
};

export interface SpaAccountCreateInput {
  name: string;
  twilio_phone_number?: string;
  timezone: string;
  business_hours: Record<string, BusinessHoursWindow[]>;
  services: SpaService[];
  staff: SpaStaffMember[];
  booking_provider: BookingProvider;
  booking_config: Record<string, unknown>;
}

export const createSpaAccount = async (payload: SpaAccountCreateInput): Promise<SpaAccount> => {
  const { data } = await apiClient.post<SpaAccount>("/api/v1/spa-accounts", payload);
  return data;
};

export const registerUser = async (payload: {
  email: string;
  password: string;
  full_name: string;
  role: UserRole;
  tenant_id: string;
}): Promise<CurrentUser> => {
  const { data } = await apiClient.post<CurrentUser>("/api/v1/auth/register", payload);
  return data;
};

export const fetchMySpaAccount = async (): Promise<SpaAccount> => {
  const { data } = await apiClient.get<SpaAccount>("/api/v1/spa-accounts/me");
  return data;
};

export const fetchSpaAccount = async (spaId: string): Promise<SpaAccount> => {
  const { data } = await apiClient.get<SpaAccount>(`/api/v1/spa-accounts/${spaId}`);
  return data;
};

export const updateSpaAccount = async (
  spaId: string,
  patch: Partial<
    Pick<
      SpaAccount,
      | "name"
      | "grok_system_prompt"
      | "business_hours"
      | "services"
      | "staff"
      | "timezone"
      | "booking_provider"
      | "twiml_voice"
    >
  >
): Promise<SpaAccount> => {
  const { data } = await apiClient.patch<SpaAccount>(`/api/v1/spa-accounts/${spaId}`, patch);
  return data;
};

// --- Contacts ---
export const fetchContacts = async (): Promise<Contact[]> => {
  const { data } = await apiClient.get<Page<Contact>>("/api/v1/contacts");
  return data.items;
};

export const createContact = async (contact: ContactInput): Promise<Contact> => {
  const { data } = await apiClient.post<Contact>("/api/v1/contacts", contact);
  return data;
};

// --- Leads (6DM Sales Agent · super_admin only) ---
export const fetchLeads = async (params?: {
  search?: string;
  booked?: boolean;
}): Promise<Page<Lead>> => {
  const { data } = await apiClient.get<Page<Lead>>("/api/v1/leads", { params });
  return data;
};

export const createLead = async (lead: ContactInput): Promise<Lead> => {
  const { data } = await apiClient.post<Lead>("/api/v1/leads", lead);
  return data;
};

// --- Calls ---
export const fetchCallLogs = async (params?: {
  direction?: CallDirection;
}): Promise<CallLog[]> => {
  const { data } = await apiClient.get<Page<CallLog>>("/api/v1/calls", { params });
  return data.items;
};

// --- Appointments ---
export const fetchAppointments = async (params?: {
  from_time?: string;
  to_time?: string;
}): Promise<Appointment[]> => {
  const { data } = await apiClient.get<Page<Appointment>>("/api/v1/appointments", { params });
  return data.items;
};

// --- Analytics ---
export const fetchAnalytics = async (): Promise<AnalyticsSummary> => {
  const { data } = await apiClient.get<AnalyticsSummary>("/api/v1/analytics");
  return data;
};

// --- Outbound calls (6DM Sales Agent · super_admin only) ---
export const initiateOutboundCall = async (
  targetPhone: string,
  callObjective?: string
): Promise<OutboundCallResponse> => {
  const { data } = await apiClient.post<OutboundCallResponse>(
    "/api/v1/telephony/voice/outbound",
    {
      to_number: targetPhone,
      call_objective: callObjective || "General appointment scheduling",
    }
  );
  return data;
};
