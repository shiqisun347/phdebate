import { useEffect, useRef, useState } from "react";
import { Room, RoomEvent, Track, type RemoteParticipant, type RemoteTrack, type RemoteTrackPublication } from "livekit-client";
import { createLiveKitToken } from "../api/client";

type LiveKitAudioStatus = "disabled" | "connecting" | "connected" | "error";

interface UseLiveKitAudioOptions {
  matchId: string;
  role: "screen" | "speaker";
  enabled: boolean;
  speakerId?: string;
  publishMicrophone?: boolean;
}

export function useLiveKitAudio(options: UseLiveKitAudioOptions): { status: LiveKitAudioStatus; error: string; audioTrackCount: number } {
  const [status, setStatus] = useState<LiveKitAudioStatus>("disabled");
  const [error, setError] = useState("");
  const [audioTrackCount, setAudioTrackCount] = useState(0);
  const attachmentsRef = useRef<HTMLMediaElement[]>([]);
  const roomRef = useRef<Room | null>(null);

  useEffect(() => {
    if (!options.enabled) {
      setStatus("disabled");
      setError("");
      setAudioTrackCount(0);
      return;
    }

    let cancelled = false;
    const room = new Room({ adaptiveStream: false, dynacast: false });
    roomRef.current = room;

    function mutePublication(publication: RemoteTrackPublication) {
      try {
        publication.setSubscribed(false);
      } catch {
        /* ignore */
      }
    }

    function syncScreenSubscription(publication: RemoteTrackPublication, participant: RemoteParticipant) {
      if (options.role !== "screen") return;
      try {
        publication.setSubscribed(shouldSubscribeToPublicationOnScreen(participant, publication));
      } catch {
        /* ignore */
      }
    }

    function attachTrack(track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) {
      if (options.role !== "screen" || track.kind !== Track.Kind.Audio) return;
      if (!shouldSubscribeToPublicationOnScreen(participant, publication)) {
        mutePublication(publication);
        return;
      }
      const element = track.attach();
      element.autoplay = true;
      element.setAttribute("data-livekit-audio", "screen");
      element.setAttribute("data-livekit-participant", participant.identity);
      element.style.display = "none";
      document.body.appendChild(element);
      attachmentsRef.current.push(element);
      setAudioTrackCount(attachmentsRef.current.length);
      void element.play().catch(() => undefined);
    }

    function detachAll() {
      for (const element of attachmentsRef.current) {
        try {
          element.pause();
          element.remove();
        } catch {
          /* ignore */
        }
      }
      attachmentsRef.current = [];
      setAudioTrackCount(0);
    }

    const onSubscribed = (track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      attachTrack(track, publication, participant);
    };

    const onPublished = (publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      syncScreenSubscription(publication, participant);
    };

    const onUnsubscribed = (track: RemoteTrack, _publication: RemoteTrackPublication, _participant: RemoteParticipant) => {
      if (options.role !== "screen" || track.kind !== Track.Kind.Audio) return;
      const detached = track.detach();
      const detachedSet = new Set(detached);
      for (const element of detached) {
        try {
          element.pause();
          element.remove();
        } catch {
          /* ignore */
        }
      }
      attachmentsRef.current = attachmentsRef.current.filter((element) => !detachedSet.has(element));
      setAudioTrackCount(attachmentsRef.current.length);
    };

    async function connect() {
      setStatus("connecting");
      setError("");
      try {
        const token = await createLiveKitToken(options.matchId, {
          role: options.role,
          speaker_id: options.speakerId,
          ttl_seconds: options.role === "screen" ? 6 * 3600 : 3600,
        });
        if (cancelled) return;
        await room.connect(token.url, token.token, { autoSubscribe: shouldAutoSubscribe(options.role) });
        if (cancelled) return;
        room.on(RoomEvent.TrackPublished, onPublished);
        room.on(RoomEvent.TrackSubscribed, onSubscribed);
        room.on(RoomEvent.TrackUnsubscribed, onUnsubscribed);
        for (const participant of room.remoteParticipants.values()) {
          for (const publication of participant.trackPublications.values()) {
            syncScreenSubscription(publication, participant);
            const track = publication.track;
            if (track) attachTrack(track as RemoteTrack, publication, participant);
          }
        }
        if (options.role === "speaker") {
          await room.localParticipant.setMicrophoneEnabled(Boolean(options.publishMicrophone));
        }
        if (!cancelled) setStatus("connected");
      } catch (err) {
        if (!cancelled) {
          setStatus("error");
          setError(err instanceof Error ? err.message : "LiveKit 连接失败");
        }
      }
    }

    void connect();

    return () => {
      cancelled = true;
      room.off(RoomEvent.TrackPublished, onPublished);
      room.off(RoomEvent.TrackSubscribed, onSubscribed);
      room.off(RoomEvent.TrackUnsubscribed, onUnsubscribed);
      void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
      if (roomRef.current === room) roomRef.current = null;
      room.disconnect();
      detachAll();
    };
  }, [options.enabled, options.matchId, options.role, options.speakerId]);

  useEffect(() => {
    if (options.role !== "speaker") return;
    const room = roomRef.current;
    if (!options.enabled || !room || status !== "connected") return;
    void room.localParticipant.setMicrophoneEnabled(Boolean(options.publishMicrophone)).catch((err: unknown) => {
      setStatus("error");
      setError(err instanceof Error ? err.message : "LiveKit 麦克风发布失败");
    });
  }, [options.enabled, options.publishMicrophone, options.role, status]);

  return { status, error, audioTrackCount };
}

export function shouldAutoSubscribe(_role: "screen" | "speaker"): boolean {
  return false;
}

type ParticipantLike = {
  identity?: string;
  metadata?: string | null;
};

type PublicationLike = {
  kind?: Track.Kind | string;
};

export function shouldPlayOnScreen(participant: ParticipantLike): boolean {
  const identity = String(participant.identity ?? "");
  const role = liveKitRoleFromMetadata(participant.metadata);
  if (role === "voice-agent" || role === "agent") return true;
  return identity === "voice-agent" || identity.startsWith("voice-agent-");
}

export function shouldSubscribeToPublicationOnScreen(participant: ParticipantLike, publication: PublicationLike): boolean {
  return publication.kind === Track.Kind.Audio && shouldPlayOnScreen(participant);
}

function liveKitRoleFromMetadata(metadata: string | null | undefined): string {
  if (!metadata) return "";
  try {
    const parsed = JSON.parse(metadata) as { role?: unknown };
    return String(parsed.role ?? "").trim().toLowerCase();
  } catch {
    return "";
  }
}
