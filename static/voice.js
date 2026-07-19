/**
 * voice.js — WebRTC mesh voice chat for a Wavelength chat room.
 *
 * Requires, in the surrounding page:
 *   - a global `socket` (Socket.IO client already connected)
 *   - a global `ROOM_ID` (int) and `USERNAME` (string)
 *
 * Add to room.html:
 *   <script>
 *     const ROOM_ID = {{ room.id }};
 *     const USERNAME = "{{ username }}";
 *   </script>
 *   <script src="{{ url_for('static', filename='voice.js') }}"></script>
 */

(function () {
  const ICE_SERVERS = [
    { urls: "stun:stun.l.google.com:19302" },
    // For reliable connectivity across real-world networks (symmetric NATs,
    // corporate firewalls, etc.) STUN alone often isn't enough. Add a TURN
    // server here in production, e.g.:
    // { urls: "turn:your-turn-server.com:3478", username: "user", credential: "pass" },
  ];

  const peers = new Map(); // sid -> { pc: RTCPeerConnection, audioEl: HTMLAudioElement, username }
  let localStream = null;
  let inVoice = false;
  let muted = false;

  const container = document.getElementById("voice-panel");
  const joinBtn = document.getElementById("voice-join-btn");
  const leaveBtn = document.getElementById("voice-leave-btn");
  const muteBtn = document.getElementById("voice-mute-btn");
  const rosterEl = document.getElementById("voice-roster");
  const remoteAudioContainer = document.getElementById("voice-remote-audio");

  function setUiJoined(joined) {
    inVoice = joined;
    if (joinBtn) joinBtn.style.display = joined ? "none" : "inline-block";
    if (leaveBtn) leaveBtn.style.display = joined ? "inline-block" : "none";
    if (muteBtn) muteBtn.style.display = joined ? "inline-block" : "none";
  }

  function renderRoster() {
    if (!rosterEl) return;
    const names = Array.from(peers.values()).map((p) => p.username);
    rosterEl.textContent = names.length
      ? `In voice: ${USERNAME} (you), ${names.join(", ")}`
      : `In voice: ${USERNAME} (you)`;
  }

  function createPeerConnection(targetSid, targetUsername) {
    const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });

    // Send our mic audio to this peer
    if (localStream) {
      localStream.getTracks().forEach((track) => pc.addTrack(track, localStream));
    }

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        socket.emit("voice_ice", {
          room_id: ROOM_ID,
          target: targetSid,
          candidate: event.candidate,
        });
      }
    };

    pc.ontrack = (event) => {
      let audioEl = document.getElementById(`voice-audio-${targetSid}`);
      if (!audioEl) {
        audioEl = document.createElement("audio");
        audioEl.id = `voice-audio-${targetSid}`;
        audioEl.autoplay = true;
        if (remoteAudioContainer) remoteAudioContainer.appendChild(audioEl);
      }
      audioEl.srcObject = event.streams[0];
    };

    pc.onconnectionstatechange = () => {
      if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
        removePeer(targetSid);
      }
    };

    peers.set(targetSid, { pc, username: targetUsername });
    renderRoster();
    return pc;
  }

  function removePeer(sid) {
    const peer = peers.get(sid);
    if (!peer) return;
    peer.pc.close();
    const audioEl = document.getElementById(`voice-audio-${sid}`);
    if (audioEl) audioEl.remove();
    peers.delete(sid);
    renderRoster();
  }

  async function joinVoice() {
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (err) {
      alert("Couldn't access your microphone: " + err.message);
      return;
    }
    setUiJoined(true);
    socket.emit("voice_join", { room_id: ROOM_ID });
  }

  function leaveVoice() {
    socket.emit("voice_leave", { room_id: ROOM_ID });
    peers.forEach((_, sid) => removePeer(sid));
    if (localStream) {
      localStream.getTracks().forEach((t) => t.stop());
      localStream = null;
    }
    setUiJoined(false);
  }

  function toggleMute() {
    if (!localStream) return;
    muted = !muted;
    localStream.getAudioTracks().forEach((track) => (track.enabled = !muted));
    if (muteBtn) muteBtn.textContent = muted ? "Unmute" : "Mute";
  }

  // --- Socket.IO signaling handlers ---

  // We just joined: told about everyone already in the voice room.
  // We initiate offers to each of them.
  socket.on("voice_peers", async ({ peers: existingPeers }) => {
    for (const { username, sid } of existingPeers) {
      const pc = createPeerConnection(sid, username);
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      socket.emit("voice_offer", { room_id: ROOM_ID, target: sid, offer });
    }
  });

  // Someone new joined after us — just wait for their offer, nothing to do yet.
  socket.on("voice_user_joined", ({ username, sid }) => {
    console.log(`${username} joined voice chat`);
  });

  socket.on("voice_user_left", ({ username, sid }) => {
    removePeer(sid);
  });

  socket.on("voice_offer", async ({ from, username, offer }) => {
    let pc = peers.get(from)?.pc;
    if (!pc) pc = createPeerConnection(from, username);
    await pc.setRemoteDescription(new RTCSessionDescription(offer));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    socket.emit("voice_answer", { room_id: ROOM_ID, target: from, answer });
  });

  socket.on("voice_answer", async ({ from, answer }) => {
    const pc = peers.get(from)?.pc;
    if (pc) await pc.setRemoteDescription(new RTCSessionDescription(answer));
  });

  socket.on("voice_ice", async ({ from, candidate }) => {
    const pc = peers.get(from)?.pc;
    if (pc && candidate) {
      try {
        await pc.addIceCandidate(new RTCIceCandidate(candidate));
      } catch (err) {
        console.warn("Failed to add ICE candidate", err);
      }
    }
  });

  // Clean up if the user navigates away without clicking "Leave"
  window.addEventListener("beforeunload", () => {
    if (inVoice) socket.emit("voice_leave", { room_id: ROOM_ID });
  });

  if (joinBtn) joinBtn.addEventListener("click", joinVoice);
  if (leaveBtn) leaveBtn.addEventListener("click", leaveVoice);
  if (muteBtn) muteBtn.addEventListener("click", toggleMute);

  setUiJoined(false);
})();
