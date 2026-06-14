import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

// ---- Types (mirror app/models.py) ----
export type OAuthStatus = "disconnected" | "connected" | "expired" | "error";
export type Status =
  | "draft" | "queued" | "rendering" | "rendered" | "review" | "approved"
  | "publishing" | "published" | "failed" | "rejected";

export interface Channel {
  id: number;
  slug: string;
  name: string;
  yt_channel_id: string | null;
  yt_channel_title: string | null;
  oauth_status: OAuthStatus;
  oauth_error: string | null;
  default_render_profile_id: number | null;
  default_skip_gate: boolean;
  default_privacy: string;
  daily_render_budget: number;
  daily_publish_budget: number;
  paused: boolean;
}

export interface Topic {
  id: number;
  channel_id: number;
  name: string;
  theme_prompt: string | null;
  content_format: "short" | "long";
  playlist_id: number | null;
  render_profile_id: number | null;
  active: boolean;
  position: number;
  video_counts: Record<Status, number>;
  video_total: number;
  playlist_title: string | null;
  playlist_yt_id: string | null;
}

export interface Playlist {
  id: number;
  channel_id: number;
  yt_playlist_id: string;
  title: string;
  description: string | null;
  privacy: string | null;
}

export interface RenderProfile {
  id: number;
  name: string;
  channel_id: number | null;
  engine: string;
  params_json: string;
}

export interface Video {
  id: number;
  channel_id: number;
  topic_id: number;
  subject: string;
  status: Status;
  position: number;
  render_profile_id: number | null;
  overrides_json: string | null;
  skip_gate: boolean | null;
  privacy: string | null;
  mpt_task_id: string | null;
  render_progress: number;
  video_path: string | null;
  thumb_path: string | null;
  script: string | null;
  title: string | null;
  description: string | null;
  tags_json: string | null;
  metadata_generated: boolean;
  approved_at: string | null;
  rejected_reason: string | null;
  yt_video_id: string | null;
  published_at: string | null;
  added_to_playlist: boolean;
  error: string | null;
  retry_count: number;
}

export interface JobRun {
  id: number;
  video_id: number | null;
  channel_id: number | null;
  kind: string;
  status: string;
  detail: string | null;
  quota_cost: number;
  created_at: string;
}

export interface DashboardActive {
  id: number;
  subject: string;
  status: Status;
  render_progress: number;
}

export interface DashboardRow {
  channel: Channel;
  counts: Record<Status, number>;
  rendered_today: number;
  published_today: number;
  quota_spent_today: number;
  quota_cap: number;
  next_publish_eta: string | null;
  active: DashboardActive[];
}

export interface ParamsOptions {
  defaults: Record<string, any>;
  video_aspect: string[];
  video_concat_mode: string[];
  video_transition_mode: (string | null)[];
  video_source: string[];
  subtitle_position: string[];
  voices: string[];
  fonts: string[];
  bgm_files: string[];
  privacy: string[];
  fields: Record<string, string>;
}

export interface AppSettings {
  render_concurrency: number;
  publish_drip_minutes: number;
  topic_autogen_enabled: boolean;
  topic_autogen_min_pending: number;
  scheduler_paused: boolean;
  mpt_base_url: string;
  youtube_quota_reset_at: string;
}

// ---- fetch wrapper ----
async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: opts.body ? { "content-type": "application/json" } : {},
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = j.detail || JSON.stringify(j);
    } catch {}
    throw new Error(msg);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

const j = (body: any) => JSON.stringify(body);

// ---- Queries ----
export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => api<any>("/health"), refetchInterval: 10000 });

export const useDashboard = () =>
  useQuery({ queryKey: ["dashboard"], queryFn: () => api<DashboardRow[]>("/dashboard"), refetchInterval: 5000 });

export const useChannels = () =>
  useQuery({ queryKey: ["channels"], queryFn: () => api<Channel[]>("/channels") });

export const useChannel = (id: number) =>
  useQuery({ queryKey: ["channel", id], queryFn: () => api<Channel>(`/channels/${id}`), enabled: !!id });

export const usePlaylists = (channelId: number) =>
  useQuery({ queryKey: ["playlists", channelId], queryFn: () => api<Playlist[]>(`/channels/${channelId}/playlists`), enabled: !!channelId });

export const useProfiles = (channelId?: number) =>
  useQuery({
    queryKey: ["profiles", channelId ?? "all"],
    queryFn: () => api<RenderProfile[]>(`/profiles${channelId ? `?channel_id=${channelId}` : ""}`),
  });

export const useParamsOptions = () =>
  useQuery({ queryKey: ["params-options"], queryFn: () => api<ParamsOptions>("/params/options"), staleTime: 60000 });

export const useTopics = (channelId: number) =>
  useQuery({
    queryKey: ["topics", channelId],
    queryFn: () => api<Topic[]>(`/topics?channel_id=${channelId}`),
    enabled: !!channelId,
    refetchInterval: 5000,
  });

export const useVideos = (channelId: number, topicId?: number) =>
  useQuery({
    queryKey: ["videos", channelId, topicId ?? "all"],
    queryFn: () => api<Video[]>(`/videos?channel_id=${channelId}${topicId ? `&topic_id=${topicId}` : ""}`),
    enabled: !!channelId,
    refetchInterval: 4000,
  });

export const useVideo = (id: number) =>
  useQuery({ queryKey: ["video", id], queryFn: () => api<Video>(`/videos/${id}`), enabled: !!id });

export const usePublishPlan = (channelId: number) =>
  useQuery({
    queryKey: ["publish-plan", channelId],
    queryFn: () => api<Record<string, string>>(`/videos/publish-plan?channel_id=${channelId}`),
    enabled: !!channelId,
    refetchInterval: 10000,
  });

export type QueueReason = { reason: string; eta: string | null };
export const useQueuePlan = (channelId: number) =>
  useQuery({
    queryKey: ["queue-plan", channelId],
    queryFn: () => api<Record<string, QueueReason>>(`/videos/queue-plan?channel_id=${channelId}`),
    enabled: !!channelId,
    refetchInterval: 5000,
  });

export const useRuns = (channelId?: number) =>
  useQuery({
    queryKey: ["runs", channelId ?? "all"],
    queryFn: () => api<JobRun[]>(`/runs?limit=60${channelId ? `&channel_id=${channelId}` : ""}`),
    refetchInterval: 6000,
  });

export const useSettings = () =>
  useQuery({ queryKey: ["settings"], queryFn: () => api<AppSettings>("/settings") });

// ---- Mutations ----
// ---- YouTube channel administration ----
export interface ChannelStats {
  subscriber_count: number;
  view_count: number;
  video_count: number;
  hidden_subscriber_count?: boolean;
}
export interface ChannelBranding {
  title: string | null;
  description: string | null;
  keywords: string | null;
  country: string | null;
  default_language: string | null;
}
export interface ChannelYoutube {
  id: string;
  title: string | null;
  thumbnail: string | null;
  statistics: ChannelStats;
  branding: ChannelBranding;
}
export interface MetricPoint {
  id: number;
  channel_id: number;
  subscriber_count: number;
  view_count: number;
  video_count: number;
  captured_at: string;
}
export interface Subscription {
  sub_id: string;
  channel_id: string;
  title: string;
  description?: string | null;
  thumbnail?: string | null;
}
export interface Subscriber {
  channel_id: string;
  title: string;
  thumbnail?: string | null;
}

export const useChannelYoutube = (id: number, enabled = true) =>
  useQuery({
    queryKey: ["yt", id],
    queryFn: () => api<ChannelYoutube>(`/channels/${id}/youtube`),
    enabled: !!id && enabled,
    retry: false,
  });

export const useMetrics = (id: number, enabled = true) =>
  useQuery({
    queryKey: ["metrics", id],
    queryFn: () =>
      api<{ latest: MetricPoint | null; history: MetricPoint[] }>(`/channels/${id}/metrics`),
    enabled: !!id && enabled,
  });

export const useSubscriptions = (id: number, enabled = true) =>
  useQuery({
    queryKey: ["subs", id],
    queryFn: () => api<Subscription[]>(`/channels/${id}/subscriptions`),
    enabled: !!id && enabled,
    retry: false,
  });

export const useSubscribers = (id: number, enabled = true) =>
  useQuery({
    queryKey: ["subscribers", id],
    queryFn: () => api<Subscriber[]>(`/channels/${id}/subscribers`),
    enabled: !!id && enabled,
    retry: false,
  });

function invalidate(qc: ReturnType<typeof useQueryClient>, keys: string[]) {
  keys.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
}

export const useMut = () => {
  const qc = useQueryClient();
  return {
    createChannel: useMutation({
      mutationFn: (b: any) => api("/channels", { method: "POST", body: j(b) }),
      onSuccess: () => invalidate(qc, ["channels", "dashboard"]),
    }),
    updateChannel: useMutation({
      mutationFn: ({ id, body }: any) => api(`/channels/${id}`, { method: "PATCH", body: j(body) }),
      onSuccess: () => invalidate(qc, ["channels", "channel", "dashboard"]),
    }),
    oauthStart: useMutation({
      mutationFn: (id: number) => api<{ auth_url: string }>(`/channels/${id}/oauth/start`, { method: "POST" }),
    }),
    oauthStatus: useMutation({
      mutationFn: (id: number) => api(`/channels/${id}/oauth-status`, { method: "GET" }),
      onSuccess: () => invalidate(qc, ["channels", "channel"]),
    }),
    disconnectChannel: useMutation({
      mutationFn: (id: number) => api(`/channels/${id}/disconnect`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["channels", "channel"]),
    }),
    uploadSecret: useMutation({
      mutationFn: ({ id, file }: { id: number; file: File }) => {
        const fd = new FormData();
        fd.append("file", file);
        return fetch(`/api/channels/${id}/credentials`, { method: "POST", body: fd }).then((r) => {
          if (!r.ok) throw new Error("upload failed");
          return r.json();
        });
      },
      onSuccess: () => invalidate(qc, ["channels", "channel"]),
    }),
    syncPlaylists: useMutation({
      mutationFn: (channelId: number) => api(`/channels/${channelId}/playlists/sync`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["playlists"]),
    }),
    createPlaylist: useMutation({
      mutationFn: ({ channelId, body }: any) => api(`/channels/${channelId}/playlists`, { method: "POST", body: j(body) }),
      onSuccess: () => invalidate(qc, ["playlists"]),
    }),
    createProfile: useMutation({
      mutationFn: (b: any) => api("/profiles", { method: "POST", body: j(b) }),
      onSuccess: () => invalidate(qc, ["profiles"]),
    }),
    updateProfile: useMutation({
      mutationFn: ({ id, body }: any) => api(`/profiles/${id}`, { method: "PATCH", body: j(body) }),
      onSuccess: () => invalidate(qc, ["profiles"]),
    }),
    deleteProfile: useMutation({
      mutationFn: (id: number) => api(`/profiles/${id}`, { method: "DELETE" }),
      onSuccess: () => invalidate(qc, ["profiles"]),
    }),
    // Topics (content themes)
    createTopic: useMutation({
      mutationFn: (b: any) => api("/topics", { method: "POST", body: j(b) }),
      onSuccess: () => invalidate(qc, ["topics", "playlists", "dashboard"]),
    }),
    updateTopic: useMutation({
      mutationFn: ({ id, body }: any) => api(`/topics/${id}`, { method: "PATCH", body: j(body) }),
      onSuccess: () => invalidate(qc, ["topics"]),
    }),
    deleteTopic: useMutation({
      mutationFn: (id: number) => api(`/topics/${id}`, { method: "DELETE" }),
      onSuccess: () => invalidate(qc, ["topics"]),
    }),
    generateVideos: useMutation({
      mutationFn: ({ id, count }: any) => api(`/topics/${id}/generate`, { method: "POST", body: j({ count }) }),
      onSuccess: () => invalidate(qc, ["topics", "videos", "dashboard"]),
    }),
    createTopicPlaylist: useMutation({
      mutationFn: (id: number) => api(`/topics/${id}/create-playlist`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["topics", "playlists"]),
    }),
    // Videos (produced units)
    createVideo: useMutation({
      mutationFn: (b: any) => api("/videos", { method: "POST", body: j(b) }),
      onSuccess: () => invalidate(qc, ["videos", "topics", "dashboard"]),
    }),
    updateVideo: useMutation({
      mutationFn: ({ id, body }: any) => api(`/videos/${id}`, { method: "PATCH", body: j(body) }),
      onSuccess: () => invalidate(qc, ["videos", "video", "dashboard"]),
    }),
    deleteVideo: useMutation({
      mutationFn: (id: number) => api(`/videos/${id}`, { method: "DELETE" }),
      onSuccess: () => invalidate(qc, ["videos", "topics", "dashboard"]),
    }),
    produceVideo: useMutation({
      mutationFn: (id: number) => api(`/videos/${id}/produce`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["videos", "dashboard"]),
    }),
    produceBulk: useMutation({
      mutationFn: (b: any) => api("/videos/produce", { method: "POST", body: j(b) }),
      onSuccess: () => invalidate(qc, ["videos", "dashboard"]),
    }),
    approveVideo: useMutation({
      mutationFn: ({ id, body }: any) => api(`/videos/${id}/approve`, { method: "POST", body: j(body || {}) }),
      onSuccess: () => invalidate(qc, ["videos", "video", "dashboard"]),
    }),
    rejectVideo: useMutation({
      mutationFn: ({ id, reason }: any) => api(`/videos/${id}/reject`, { method: "POST", body: j({ reason }) }),
      onSuccess: () => invalidate(qc, ["videos", "video", "dashboard"]),
    }),
    requeueVideo: useMutation({
      mutationFn: (id: number) => api(`/videos/${id}/requeue`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["videos", "dashboard"]),
    }),
    retryVideo: useMutation({
      mutationFn: (id: number) => api(`/videos/${id}/retry`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["videos", "dashboard"]),
    }),
    regenMeta: useMutation({
      mutationFn: (id: number) => api(`/videos/${id}/regenerate-metadata`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["video", "videos"]),
    }),
    updateSettings: useMutation({
      mutationFn: (b: any) => api("/settings", { method: "PATCH", body: j(b) }),
      onSuccess: () => invalidate(qc, ["settings"]),
    }),
    // YouTube channel administration
    updateBranding: useMutation({
      mutationFn: ({ id, body }: any) => api(`/channels/${id}/branding`, { method: "PUT", body: j(body) }),
      onSuccess: () => invalidate(qc, ["yt"]),
    }),
    refreshMetrics: useMutation({
      mutationFn: (id: number) => api(`/channels/${id}/metrics/refresh`, { method: "POST" }),
      onSuccess: () => invalidate(qc, ["metrics", "yt"]),
    }),
    subscribe: useMutation({
      mutationFn: ({ id, channel }: any) => api(`/channels/${id}/subscriptions`, { method: "POST", body: j({ channel }) }),
      onSuccess: () => invalidate(qc, ["subs"]),
    }),
    unsubscribe: useMutation({
      mutationFn: ({ id, subId }: any) => api(`/channels/${id}/subscriptions/${subId}`, { method: "DELETE" }),
      onSuccess: () => invalidate(qc, ["subs"]),
    }),
  };
};
