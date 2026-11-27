// Both callers use the pulls list (not search) so closed PRs stop blocking immediately.
export async function pendingPortalPR(repository, pkg, token, request = fetch) {
  const root = `https://api.github.com/repos/${repository}/pulls`;
  const headers = { Authorization: `Bearer ${token}`, "User-Agent": "cardputerzero-dev-portal" };
  async function* pages(url) {
    for (let page = 1; ; page++) {
      const response = await request(`${url}${url.includes("?") ? "&" : "?"}per_page=100&page=${page}`, { headers });
      if (!response.ok) throw new Error(`Cannot check pending submissions (GitHub ${response.status})`);
      const items = await response.json();
      yield* items;
      if (items.length < 100) return;
    }
  }

  for await (const pr of pages(`${root}?state=open&base=main`)) {
    const branch = pr.head.ref;
    if (pr.head.repo?.full_name.toLowerCase() !== repository.toLowerCase()) continue;
    if (!/-web-[0-9]+$/.test(branch)) continue;
    if (branch.replace(/-web-[0-9]+$/, "") === `metadata/${pkg}`) return pr.number;
    if (!branch.startsWith(`publish/${pkg}-`)) continue;
    // app vs app-pro: the hyphen is legal in both package names and versions.
    for await (const file of pages(`${root}/${pr.number}/files`)) {
      if (file.filename.startsWith(`pool/main/${pkg}/`)) return pr.number;
    }
  }
  return null;
}
