const player = { queue: [], index: 0 };
function playQueue(queue, index = 0) {
  Object.assign(player, { queue, index });
  const item = queue[index];
  const audio = $("#player");
  audio.src = `/api/files/${path(item.id)}/preview`;
  $("#player-bar").hidden = false;
  $("#player-title").textContent = item.title || "Preview";
  $("#player-subtitle").textContent = [item.artist, queue.length > 1 ? `${index + 1} of ${queue.length}` : ""].filter(Boolean).join(" · ");
  $("#player-previous").hidden = $("#player-next").hidden = queue.length < 2;
  $("#player-previous").disabled = index === 0;
  $("#player-next").disabled = index === queue.length - 1;
  audio.play().catch(() => toast("Press play to preview this file."));
}
const stepQueue = (step) => {
  const index = player.index + step;
  if (index >= 0 && index < player.queue.length) playQueue(player.queue, index);
};
$("#player-previous").addEventListener("click", () => stepQueue(-1));
$("#player-next").addEventListener("click", () => stepQueue(1));
$("#player").addEventListener("ended", () => stepQueue(1));
$("#close-player").addEventListener("click", () => {
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player").load();
  $("#player-bar").hidden = true;
});
$("#player").addEventListener("error", () =>
  toast(
    "Audio preview could not be played. You can download the original file.",
    true,
  ),
);
document.addEventListener("click", (e) => {
  const button = e.target.closest("button[data-preview]");
  if (!button) return;
  playQueue([
    {
      id: button.dataset.preview,
      title:
        button.dataset.title ||
        button.closest("article, section")?.querySelector("h3")?.textContent,
      artist: button.dataset.artist,
    },
  ]);
});
logoutHooks.push(() => {
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player-bar").hidden = true;
});
