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

  useEffect(() => {
    if (!options.enabled) {
      setStatus("disabled");
      setError("");
      setAudioTrackCount(0);
      return;
    }

    let cancelled = false;
    const room = new Room({ adaptiveStream: false, dynacast: false });

    function attachTrack(track: RemoteTrack) {
      if (options.role !== "screen" || track.kind !== Track.Kind.Audio) return;
      const element = track.attach();
      element.autoplay = true;
      element.setAttribute("data-livekit-audio", "screen");
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

    const onSubscribed = (track: RemoteTrack, _publication: RemoteTrackPublication, _participant: RemoteParticipant) => {
      attachTrack(track);
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
        await room.connect(token.url, token.token, { autoSubscribe: true });
        if (cancelled) return;
        room.on(RoomEvent.TrackSubscribed, onSubscribed);
        room.on(RoomEvent.TrackUnsubscribed, onUnsubscribed);
        for (const participant of room.remoteParticipants.values()) {
          for (const publication of participant.trackPublications.values()) {
            const track = publication.track;
            if (track) attachTrack(track as RemoteTrack);
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
      room.off(RoomEvent.TrackSubscribed, onSubscribed);
      room.off(RoomEvent.TrackUnsubscribed, onUnsubscribed);
      void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
      room.disconnect();
      detachAll();
    };
  }, [options.enabled, options.matchId, options.publishMicrophone, options.role, options.speakerId]);

  return { status, error, audioTrackCount };
}
