import { useState } from "react";
import { api } from "../api";
import { IconThumbDown, IconThumbUp } from "./Icons";
import type { Message } from "../types";

interface Props {
  conversationId: string | null;
  message: Message;
}

/**
 * Was this answer any good?
 *
 * The counts are the whole point in a shared workspace: several analysts read
 * the same answer, and "three trusted this, one did not" is a stronger signal
 * than any one opinion. Your own vote is shown as cast, everyone else's as a
 * count beside it.
 */
export function FeedbackButtons({ conversationId, message }: Props) {
  const [mine, setMine] = useState(message.my_feedback ?? null);
  const [up, setUp] = useState(message.feedback_up ?? 0);
  const [down, setDown] = useState(message.feedback_down ?? 0);
  const [busy, setBusy] = useState(false);

  if (!conversationId) return null;

  const vote = async (rating: "up" | "down") => {
    if (busy) return;
    setBusy(true);
    try {
      // Clicking the vote you already cast withdraws it, which is what every
      // other thumbs control does.
      const next = mine === rating ? null : rating;
      const tally = await api.rateMessage(conversationId, message.id, next);
      setMine(tally.mine);
      setUp(tally.up);
      setDown(tally.down);
    } catch {
      // A lost vote is not worth interrupting an investigation over.
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        className={`turn-action ${mine === "up" ? "voted" : ""}`}
        title="This answer was useful"
        aria-label="This answer was useful"
        onClick={() => vote("up")}
      >
        <IconThumbUp size={15} />
        {up > 0 && <span className="vote-count">{up}</span>}
      </button>
      <button
        className={`turn-action ${mine === "down" ? "voted down" : ""}`}
        title="This answer was wrong or unhelpful"
        aria-label="This answer was wrong or unhelpful"
        onClick={() => vote("down")}
      >
        <IconThumbDown size={15} />
        {down > 0 && <span className="vote-count">{down}</span>}
      </button>
    </>
  );
}
