import { useState } from "react";
import { askChat, isChatReady } from "../askChat";

/**
 * A button that sends a file, tab or run to the chat agent as the start of a
 * question. With a conversation open it asks which one; without, it starts a
 * new conversation straight away.
 */
export function AskChatButton({ reference, label = "Ask chat agent about this" }: {
  reference: string;
  label?: string;
}) {
  const [choosing, setChoosing] = useState(false);
  if (!reference) return null;
  const click = () => {
    if (!isChatReady()) {
      askChat(reference, "new");
      return;
    }
    setChoosing((v) => !v);
  };
  return (
    <span className="ask-chat">
      <button type="button" onClick={click} title={`Ask the chat agent about ${reference}`}>
        {label}
      </button>
      {choosing && (
        <span className="ask-chat-choices" role="group" aria-label="Where to ask">
          <button
            type="button"
            title="Add the reference to the open conversation. Nothing is sent until you press Send."
            onClick={() => { setChoosing(false); askChat(reference, "current"); }}
          >
            Ask in current conversation
          </button>
          <button
            type="button"
            title="Start a new conversation with the reference. Nothing is sent until you press Send."
            onClick={() => { setChoosing(false); askChat(reference, "new"); }}
          >
            Ask in new conversation
          </button>
        </span>
      )}
    </span>
  );
}
