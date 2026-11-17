/* Pepito native image UI. Integration target: managed Open WebUI 0.12.0.
 * ResponseMessage: #message-{id}, srcdoc embeds, direct per-file Image wrappers.
 * MessageInput: #message-input-container, outside the ProseMirror editor.
 * Chat: /api/chat/completions, chat_id + user_message + features.image_generation.
 * This extension never changes completion bodies. It submits a prompt only when the user
 * clicks Regerar in the Prompt panel: one fixed message, through the composer.
 */
(() => {
  'use strict';
  if (window.__pepitoImageUI) return;
  window.__pepitoImageUI = Object.freeze({ version: '1.2.0', openWebUI: '0.12.0' });

  const API = '/api/v1/pepito/images';
  const originalFetch = window.fetch;
  const fetchNative = originalFetch.bind(window);
  const MAX_PIXELS = 16777216;
  const MAX_SIDE = 8192;
  const MAX_IMAGE_BYTES = 32 * 1024 * 1024;
  const MAX_MASK_BYTES = 8 * 1024 * 1024;
  const MAX_UNDO = 40;
  const MAX_POINTS = 32768;
  const mounts = new Set();
  const frames = new WeakMap();
  const sources = new Map();
  const selections = new Map();
  const accepted = new Map();
  const preferences = new Map();
  let viewer = null;
  let composerSnapshot = null;
  let recovery = null;
  let fallbackChat = null;
  let draft = null;
  let chip = null;
  let editor = null;
  let sending = false;
  let scanQueued = false;
  let observedChat = '';
  let observer;
  let visibility;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function button(text, action, className = '') {
    const element = node('button', className, text);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
  }

  function currentChat() {
    const match = location.pathname.match(/^\/c\/([^/]+)\/?$/);
    try {
      if (match) return decodeURIComponent(match[1]);
      if (fallbackChat?.path === location.pathname &&
          document.getElementById(`message-${fallbackChat.messageId}`)) return fallbackChat.chatId;
    } catch { /* A malformed route cannot identify an owned chat. */ }
    return '';
  }

  function authHeaders(json = false) {
    const token = localStorage.getItem('token');
    if (!token) throw new Error('Sua sessão expirou. Entre novamente antes de editar a imagem.');
    return { Authorization: `Bearer ${token}`, ...(json ? { 'Content-Type': 'application/json' } : {}) };
  }

  async function failure(response) {
    try {
      const body = await response.json();
      const detail = body.detail ?? body.error?.message ?? body.message;
      if (typeof detail === 'string') return detail;
      if (typeof detail?.message === 'string') return detail.message;
    } catch { /* A proxy may return a non-JSON error. */ }
    return `Não foi possível concluir a operação (${response.status}). Tente novamente.`;
  }

  async function api(path, options = {}) {
    const response = await fetchNative(`${API}${path}`, {
      ...options, headers: authHeaders(options.body !== undefined),
      credentials: 'same-origin', redirect: 'error'
    });
    if (!response.ok) throw new Error(await failure(response));
    return response.json();
  }

  let noticeTimer = 0;
  function announce(message, alert = false) {
    let status = document.getElementById('pepito-image-notice');
    if (!status) {
      status = node('div', 'pepito-image-notice');
      status.id = 'pepito-image-notice';
      document.body.append(status);
    }
    status.setAttribute('role', alert ? 'alert' : 'status');
    status.setAttribute('aria-live', alert ? 'assertive' : 'polite');
    status.textContent = message;
    status.hidden = !message;
    // A notice that never leaves would sit over what it tells the user to use.
    clearTimeout(noticeTimer);
    if (message) noticeTimer = setTimeout(() => { status.hidden = true; }, alert ? 9000 : 5000);
  }

  function report(error) {
    announce(error instanceof Error ? error.message : String(error), true);
  }

  function canonical(url) {
    try {
      const parsed = new URL(url, location.origin);
      return parsed.origin === location.origin ? parsed.pathname + parsed.search : '';
    } catch { return ''; }
  }

  async function limitedBlob(response, maximum) {
    if (!response.ok) throw new Error(await failure(response));
    if (Number(response.headers.get('content-length')) > maximum) {
      throw new Error('A imagem excede o limite do editor. Use uma imagem de até 32 MiB.');
    }
    if (!response.body) {
      const blob = await response.blob();
      if (blob.size > maximum) throw new Error('A imagem excede o limite de 32 MiB do editor.');
      return blob;
    }
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > maximum) {
          await reader.cancel();
          throw new Error('A imagem excede o limite de 32 MiB do editor.');
        }
        chunks.push(value);
      }
      return new Blob(chunks, { type: response.headers.get('content-type') || 'application/octet-stream' });
    } finally { reader.releaseLock(); }
  }

  /* Decoded source cache.
   * A resource is one object URL for one picture version, keyed by
   * chat_id + number + file id (the content version named by the gallery entry;
   * a number is never rebound to other bytes). Holders (gallery, viewer, editor,
   * mask chip) take refs, so nobody revokes another holder's URL. When the last
   * ref goes, the resource stays decoded as an idle LRU entry instead of being
   * revoked: scrolling a gallery back into view or remounting it reuses the URL
   * without another /source request. `sources` is a Map, so insertion order is the
   * LRU order; every use moves the key to the end. Only idle entries are evicted,
   * oldest first, while the pinned blob bytes exceed CACHE_BYTES. Idle entries are
   * also revoked on chat change (purgeSources in scan), on page hide, and at once
   * when released while their chat is not the one on screen. Failed loads are
   * never cached. Bytes are the blobs pinned by object URLs; decoded bitmaps
   * belong to the browser's own image cache. */
  const CACHE_BYTES = 256 * 1024 * 1024;
  const MAX_ACTIVE_SOURCES = 6;
  const FILE_CONTENT = /^\/api\/v1\/files\/([A-Za-z0-9_-]{1,128})\/content$/;
  let cachedBytes = 0;

  function versionOf(chatId, number) {
    for (const mount of mounts) {
      if (mount.chatId !== chatId || !mount.entries) continue;
      const entry = mount.entries.find(item => item.number === number);
      const match = entry && FILE_CONTENT.exec(canonical(entry.url));
      if (match) return match[1];
    }
    return '';
  }

  function dropSource(resource) {
    if (sources.get(resource.key) === resource) sources.delete(resource.key);
    if (resource.url) URL.revokeObjectURL(resource.url);
    resource.url = '';
    cachedBytes -= resource.bytes;
    resource.bytes = 0;
  }

  function trimSources() {
    for (const resource of sources.values()) {
      if (cachedBytes <= CACHE_BYTES) return;
      if (resource.refs === 0 && resource.state === 'ready') dropSource(resource);
    }
  }

  function purgeSources(keep = () => false) {
    for (const resource of [...sources.values()]) {
      if (resource.refs === 0 && resource.state === 'ready' && !keep(resource)) dropSource(resource);
    }
  }

  // Idle entries survive only for the conversation on screen.
  function settleIdle(resource) {
    if (resource.refs > 0 || resource.state === 'loading') return;
    if (resource.state !== 'ready' || resource.chatId !== currentChat()) dropSource(resource);
    else trimSources();
  }

  async function acquireSource(chatId, number) {
    const version = versionOf(chatId, number);
    const key = `${chatId}\n${number}\n${version}`;
    let resource = sources.get(key);
    if (resource) {
      sources.delete(key);
      sources.set(key, resource);
    } else {
      let active = 0;
      for (const item of sources.values()) if (item.refs > 0) active += 1;
      if (active >= MAX_ACTIVE_SOURCES) {
        throw new Error('Há muitas imagens abertas. Feche o editor ou role até esta galeria e tente novamente.');
      }
      resource = { key, chatId, refs: 0, url: '', bytes: 0, width: 0, height: 0, state: 'loading' };
      resource.ready = (async () => {
        try {
          const query = `chat_id=${encodeURIComponent(chatId)}${version ? `&v=${encodeURIComponent(version)}` : ''}`;
          // The server's private long-lived validators let the browser's HTTP cache
          // answer a remount after this decoded entry was evicted.
          const response = await fetchNative(`${API}/source/${number}?${query}`, {
            headers: authHeaders(), credentials: 'same-origin', redirect: 'error'
          });
          const blob = await limitedBlob(response, MAX_IMAGE_BYTES);
          if (!/^image\//i.test(blob.type)) throw new Error('A origem não é uma imagem válida.');
          resource.url = URL.createObjectURL(blob);
          resource.bytes = blob.size;
          cachedBytes += blob.size;
          const image = new Image();
          image.src = resource.url;
          await image.decode();
          resource.width = image.naturalWidth;
          resource.height = image.naturalHeight;
          if (!resource.width || !resource.height || resource.width > MAX_SIDE || resource.height > MAX_SIDE ||
              resource.width * resource.height > MAX_PIXELS) {
            throw new Error('O editor aceita imagens de até 8192 px por lado e 16 megapixels.');
          }
          resource.state = 'ready';
          return resource;
        } catch (error) {
          resource.state = 'failed';
          dropSource(resource);
          throw error;
        } finally {
          settleIdle(resource);
        }
      })();
      sources.set(key, resource);
    }
    resource.refs += 1;
    try { await resource.ready; return resource; }
    catch (error) { releaseSource(resource); throw error; }
  }

  function retainSource(resource) { resource.refs += 1; return resource; }

  function releaseSource(resource) {
    if (!resource) return;
    resource.refs -= 1;
    settleIdle(resource);
  }

  function setSelection(chatId, number) {
    selections.set(chatId, number);
    for (const mount of mounts) if (mount.chatId === chatId) mount.updateLabels?.();
  }

  function assertCanSelect(chatId, number) {
    if (sending) throw new Error('Aguarde o envio terminar antes de trocar a imagem.');
    if (draft && draft.chatId === chatId && draft.number !== number) {
      throw new Error(`A área anexada pertence à imagem ${draft.number}. Remova essa área antes de escolher outra imagem.`);
    }
  }

  async function select(chatId, number) {
    assertCanSelect(chatId, number);
    const result = await api('/selection', { method: 'POST', body: JSON.stringify({ chat_id: chatId, number }) });
    setSelection(chatId, result.selected_number);
  }

  // SPEC-319: the favourite of a two-picture gallery; the server also selects it.
  async function prefer(chatId, number) {
    assertCanSelect(chatId, number);
    return api('/preference', { method: 'POST', body: JSON.stringify({ chat_id: chatId, number }) });
  }

  // The preferred picture is shown first; only two-picture galleries have a preference.
  function arrangeEntries(entries, preferred) {
    if (entries.length !== 2 || preferred === null || preferred === undefined) return entries;
    return [...entries].sort((a, b) => (b.number === preferred) - (a.number === preferred));
  }

  function markerOf(frame) {
    const html = frame.getAttribute('srcdoc');
    if (!html || !html.includes('data-pepito-gallery')) return '';
    const marker = new DOMParser().parseFromString(html, 'text/html').querySelector('[data-pepito-gallery]');
    const group = marker?.getAttribute('data-pepito-gallery') || '';
    return /^[a-zA-Z0-9_-]{1,128}$/.test(group) ? group : '';
  }

  function hideStock(mount) {
    if (!mount.ready) return;
    const message = document.getElementById(`message-${mount.messageId}`);
    if (!message || !message.contains(mount.frame)) return;
    const urls = new Set(mount.entries.map(entry => canonical(entry.url)).filter(Boolean));
    for (const image of message.querySelectorAll('img[data-cy="image"]')) {
      if (image.closest('.pepito-gallery') || !urls.has(canonical(image.getAttribute('src')))) continue;
      const imageRoot = image.closest('div.relative.group.w-fit');
      const wrapper = imageRoot?.parentElement;
      const list = wrapper?.parentElement;
      // Only ResponseMessage's direct per-file wrappers, not markdown/attachments elsewhere.
      if (wrapper?.tagName !== 'DIV' || !list?.classList.contains('overflow-x-auto') ||
          !list.classList.contains('flex-wrap') || !list.classList.contains('my-1') ||
          wrapper.querySelectorAll('img').length !== 1 || wrapper.querySelector('iframe')) continue;
      if (!wrapper.classList.contains('pepito-stock-hidden')) {
        wrapper.classList.add('pepito-stock-hidden');
        mount.hidden.add(wrapper);
      }
    }
  }

  function closeViewerFor(mount) {
    if (viewer?.mount === mount && viewer.dialog.open) viewer.dialog.close();
  }

  function destroyMount(mount) {
    closeViewerFor(mount);
    visibility?.unobserve(mount.frame);
    if (mount.section) {
      visibility?.unobserve(mount.section);
      frames.delete(mount.section);
    }
    mount.sequence = (mount.sequence || 0) + 1;
    releaseSource(mount.resource);
    releaseSource(mount.extra);
    mount.resource = mount.extra = null;
    mount.section?.remove();
    mount.error?.remove();
    mount.frame.classList.remove('pepito-stock-hidden');
    for (const wrapper of mount.hidden) wrapper.classList.remove('pepito-stock-hidden');
    mounts.delete(mount);
    frames.delete(mount.frame);
  }

  async function upgrade(mount) {
    if (mount.state === 'loading' || mount.state === 'mounted') return;
    mount.state = 'loading';
    mount.error?.remove();
    mount.error = null;
    try {
      const query = mount.chatId ? `?chat_id=${encodeURIComponent(mount.chatId)}` : '';
      const data = await api(`/gallery/${encodeURIComponent(mount.group)}${query}`);
      if (typeof data.chat_id !== 'string' || !data.chat_id ||
          (mount.chatId && data.chat_id !== mount.chatId) || data.message_id !== mount.messageId || data.group_id !== mount.group ||
          !Array.isArray(data.entries) || !data.entries.length || data.entries.length > 100) {
        throw new Error('A galeria não corresponde a esta mensagem. Recarregue a conversa.');
      }
      const numbers = new Set();
      for (const entry of data.entries) {
        if (!Number.isSafeInteger(entry.number) || entry.number < 1 || numbers.has(entry.number) || !canonical(entry.url)) {
          throw new Error('Os dados da galeria são inválidos. Recarregue a conversa.');
        }
        numbers.add(entry.number);
      }
      if (!mount.frame.isConnected || location.pathname !== mount.path) return;
      const activeChat = currentChat();
      if (activeChat && activeChat !== data.chat_id) throw new Error('Esta galeria pertence a outra conversa.');
      mount.chatId = data.chat_id;
      if (!activeChat) fallbackChat = { path: mount.path, chatId: data.chat_id, messageId: mount.messageId };
      mount.preferred = data.entries.length === 2 && data.entries.some(entry => entry.number === data.preferred_number)
        ? data.preferred_number : null;
      mount.compare = data.entries.length === 2 && mount.preferred === null;
      mount.entries = arrangeEntries(data.entries, mount.preferred);
      setSelection(mount.chatId, data.explicit_selected_number);
      accepted.set(mount.chatId, data.accepted_number);
      buildGallery(mount);
      if (!preferences.has(mount.chatId)) loadPreferences(mount.chatId);
      await mount.show(0);
      if (!mount.frame.isConnected || !mount.resource) {
        mount.section?.remove();
        return;
      }
      mount.ready = true;
      mount.state = 'mounted';
      // One synchronous swap: the stock preview (already held to the gallery's box by
      // CSS) leaves in the same frame the sized gallery appears, so nothing reflows.
      mount.section.hidden = false;
      mount.frame.classList.add('pepito-stock-hidden');
      hideStock(mount);
      visibility.unobserve(mount.frame);
      frames.set(mount.section, mount);
      visibility.observe(mount.section);
    } catch (error) {
      releaseSource(mount.resource);
      releaseSource(mount.extra);
      mount.resource = mount.extra = null;
      mount.section?.remove();
      mount.state = 'failed';
      if (!mount.frame.isConnected) return;
      mount.error = node('div', 'pepito-gallery-error');
      const text = node('p', '', `Não foi possível abrir a galeria. ${error.message}`);
      mount.error.append(text, button('Tentar novamente', () => upgrade(mount)));
      mount.frame.after(mount.error);
      // Stock previews stay untouched if metadata or original-image loading failed.
    }
  }

  const NO_SEED = 'Esta imagem não guardou a semente; use Regerar (nova semente) no Prompt.';
  // `uploadsOnly`: a picture the user attached has no number here, so only Download applies.
  // `onPrompt` opens the Prompt panel of a gallery picture (SPEC-308); uploads have no recipe.
  // `closeViewer` closes the viewer the menu lives in before a regeneration is sent.
  function imageActions(chatId, current, opener, uploadsOnly = false, onPrompt = null, closeViewer = () => {}, compare = null) {
    const details = node('details', 'pepito-image-menu');
    const summary = node('summary', 'pepito-more', '⋯');
    summary.setAttribute('aria-label', 'Opções da imagem');
    const panel = node('div', 'pepito-menu-panel');
    const status = node('p', 'pepito-menu-status');
    status.setAttribute('role', 'status');
    const run = async (control, action) => {
      control.disabled = true;
      status.textContent = '';
      try { await action(); }
      catch (error) { status.textContent = error.message; }
      finally { control.disabled = false; }
    };
    const download = button('Download', () => {
      const { entry, resource } = current();
      if (!resource?.url) { status.textContent = 'Aguarde a imagem carregar.'; return; }
      const link = node('a');
      link.href = resource.url;
      link.download = current().name || `imagem-${entry.number}.png`;
      link.click();
      details.open = false;
    }, 'pepito-download');
    const compareItem = button('Comparar lado a lado', () => {
      details.open = false;
      compare.run().catch(report);
    }, 'pepito-compare-images');
    compareItem.hidden = true;
    const use = button('Usar esta', () => run(use, async () => {
      await select(chatId, current().entry.number);
      status.textContent = 'Base selecionada. Nenhuma mensagem foi enviada.';
      refresh();
    }), 'pepito-use-image');
    const approve = button('Aprovar resultado', () => run(approve, async () => {
      const number = current().entry.number;
      const result = await api('/acceptance', {
        method: 'POST', body: JSON.stringify({ chat_id: chatId, number, verdict: 'accepted' })
      });
      accepted.set(chatId, result.accepted_number);
      for (const mount of mounts) if (mount.chatId === chatId) mount.updateLabels?.();
      status.textContent = 'Resultado aprovado. A base selecionada não foi alterada.';
      refresh();
    }), 'pepito-approve-image');
    const edit = button('Editar área', () => {
      details.open = false;
      openEditor(chatId, current().entry.number, opener || summary).catch(report);
    });
    // What made this picture: its prompt, part, sources, area and seed, and Regerar.
    const prompt = button('Prompt', () => {
      details.open = false;
      onPrompt();
    }, 'pepito-prompt-image');
    // The same recipe and the recorded seed (SPEC-317), straight from the menu.
    const reseed = button('Regerar (mesma semente)', () => run(reseed, async () => {
      const number = current().entry.number;
      const recipe = await api(`/recipe/${number}?chat_id=${encodeURIComponent(chatId)}`);
      if (!recipe.regenerable) throw new Error('Esta receita não pode ser refeita: uma origem dela não está mais no chat.');
      if (recipe.seed === null || recipe.seed === undefined) throw new Error(NO_SEED);
      await sendRegeneration({ chatId, sent: false }, { chat_id: chatId, number, mode: 'seed', step: recipe.step }, () => {
        details.open = false;
        closeViewer();
      });
    }), 'pepito-reseed-image');
    // What the model drew before the area was pasted (SPEC-318): shown, nothing is sent.
    const raw = button('Ver sem colagem', () => run(raw, async () => {
      const number = current().entry.number;
      const resource = await loadRaw(chatId, number);
      details.open = false;
      await showRaw(chatId, number, resource, opener || summary);
    }), 'pepito-raw-image');
    // The recorded prompt and seed on the whole picture, no area (SPEC-318).
    const whole = button('Refazer na imagem inteira', () => run(whole, async () => {
      const number = current().entry.number;
      const recipe = await api(`/recipe/${number}?chat_id=${encodeURIComponent(chatId)}`);
      if (!recipe.regenerable) throw new Error('Esta receita não pode ser refeita: uma origem dela não está mais no chat.');
      if (!recipe.area_png) throw new Error('Só imagens editadas numa área podem ser refeitas inteiras.');
      await sendRegeneration({ chatId, sent: false }, { chat_id: chatId, number, mode: 'whole', step: recipe.step }, () => {
        details.open = false;
        closeViewer();
      });
    }), 'pepito-whole-image');
    raw.hidden = whole.hidden = true;
    const settings = node('details', 'pepito-editor-preferences');
    settings.append(node('summary', '', 'Preferências do editor'));
    const form = node('form', 'pepito-preferences-form');
    const autoLabel = node('label', 'pepito-auto-region');
    const auto = node('input');
    auto.type = 'checkbox';
    auto.name = 'auto_region';
    autoLabel.append(auto, node('span', '', 'Selecionar região automaticamente'));
    const modelLabel = node('label', '', 'Modelo de edição instalado');
    const model = node('select');
    model.name = 'editor_model_id';
    modelLabel.append(model);
    const methodLabels = {
      auto: 'Automático', qwen: 'Qwen', warp: 'Deformação',
      warp_qwen: 'Deformação + Qwen'
    };
    const methodLabel = node('label', '', 'Método de edição');
    const method = node('select');
    method.name = 'edit_method';
    for (const [value, label] of Object.entries(methodLabels)) {
      const option = node('option', '', label);
      option.value = value;
      method.append(option);
    }
    methodLabel.append(method);
    const seedLabel = node('label', '', 'Seed de comparação');
    const seed = node('input');
    seed.type = 'text'; seed.inputMode = 'numeric'; seed.pattern = '[0-9]*'; seed.maxLength = 19; seed.autocomplete = 'off';
    seed.name = 'comparison_seed'; seed.placeholder = 'Automática';
    seedLabel.append(seed);
    const help = node('p', 'pepito-helper', 'A escolha vale para a próxima edição. Em mudanças locais de forma com uma única imagem, o método tem prioridade sobre o modelo instalado; cor e fundo continuam usando esse modelo. O Automático olha a imagem: em arte usa Deformação + Qwen, depois Deformação e depois Qwen quando você diz que nada mudou; em foto usa só Qwen. Métodos escolhidos sem dependências instaladas não são substituídos silenciosamente. Uma seed fixa mantém a mesma base, área e pedido nas comparações.');
    const save = button('Salvar preferências', () => {});
    save.type = 'submit';
    const settingsStatus = node('p', 'pepito-menu-status');
    settingsStatus.setAttribute('role', 'status');
    const reset = button('Zerar calibragem aprendida', async () => {
      if (!window.confirm('Zerar os ajustes aprendidos desta conta? As imagens e conversas serão mantidas.')) return;
      reset.disabled = true;
      try {
        await api('/calibration/reset', { method: 'POST' });
        settingsStatus.textContent = 'Calibragem zerada somente para esta conta.';
      } catch (error) { settingsStatus.textContent = error.message; }
      finally { reset.disabled = false; }
    }, 'pepito-reset-calibration');
    form.append(autoLabel, methodLabel, modelLabel, seedLabel, help, save, reset, settingsStatus);
    settings.append(form);
    let loaded = false;
    let loading = false;
    let baseline = null;
    const fill = data => {
      auto.checked = data.auto_region;
      model.replaceChildren();
      const automatic = node('option', '', 'Automático (roteamento atual)');
      automatic.value = '';
      model.append(automatic);
      const listed = new Set();
      for (const item of data.editors || []) {
        const option = node('option', '', item.label || item.model_id);
        option.value = item.model_id;
        model.append(option);
        listed.add(item.model_id);
      }
      if (data.editor_model_id && !listed.has(data.editor_model_id)) {
        const missing = node('option', '', `${data.editor_model_id} (indisponível)`);
        missing.value = data.editor_model_id;
        model.append(missing);
      }
      model.value = data.editor_model_id || '';
      method.value = data.edit_method || 'auto';
      seed.value = data.comparison_seed === null ? '' : String(data.comparison_seed);
      baseline = { auto_region: data.auto_region, editor_model_id: data.editor_model_id || '', edit_method: method.value, seed: seed.value };
      loaded = true;
    };
    settings.addEventListener('toggle', async () => {
      if (!settings.open || loading) return;
      loading = true;
      save.disabled = true;
      settingsStatus.textContent = 'Carregando preferências…';
      try {
        const data = await api(`/preferences?chat_id=${encodeURIComponent(chatId)}`);
        preferences.set(chatId, data);
        fill(data);
        settingsStatus.textContent = '';
        renderPreferenceChip();
      } catch (error) { settingsStatus.textContent = `${error.message} Feche e reabra para tentar novamente.`; }
      finally { loading = false; save.disabled = !loaded; }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!loaded || loading) return;
      const seedText = seed.value.trim();
      if (seedText !== '' && (!/^\d{1,19}$/.test(seedText) || BigInt(seedText) > 9223372036854775807n)) {
        settingsStatus.textContent = 'Use uma seed inteira entre 0 e 9223372036854775807, ou deixe vazio.';
        return;
      }
      const fields = [];
      if (auto.checked !== baseline.auto_region) fields.push(`"auto_region":${auto.checked}`);
      if (model.value !== baseline.editor_model_id) fields.push(`"editor_model_id":${model.value ? JSON.stringify(model.value) : 'null'}`);
      if (method.value !== baseline.edit_method) fields.push(`"edit_method":${JSON.stringify(method.value)}`);
      if (seedText !== baseline.seed) fields.push(`"comparison_seed":${seedText === '' ? 'null' : BigInt(seedText).toString()}`);
      if (!fields.length) { settingsStatus.textContent = 'Nenhuma alteração para salvar.'; return; }
      save.disabled = true;
      try {
        // Only changed fields travel; the seed is serialized as text to keep 63-bit precision.
        const data = await api('/preferences', { method: 'POST', body: `{"chat_id":${JSON.stringify(chatId)},${fields.join(',')}}` });
        preferences.set(chatId, data);
        fill(data);
        renderPreferenceChip();
        settingsStatus.textContent = 'Preferências salvas para a próxima edição.';
      } catch (error) { settingsStatus.textContent = error.message; }
      finally { save.disabled = false; }
    });
    panel.append(...(uploadsOnly ? [download] : [download, compareItem, use, approve, edit, prompt, reseed, raw, whole, settings]), status);
    details.append(summary, panel);
    const refresh = () => {
      if (uploadsOnly) { download.disabled = !current().resource; return; }
      const number = current().entry.number;
      use.textContent = selections.get(chatId) === number ? 'Usar esta · selecionada' : 'Usar esta';
      approve.textContent = accepted.get(chatId) === number ? 'Aprovar resultado · aprovado' : 'Aprovar resultado';
      approve.hidden = !current().entry.attempt_id;
      compareItem.hidden = !compare?.available();
      download.disabled = use.disabled = approve.disabled = edit.disabled = !current().resource || sending;
      prompt.disabled = reseed.disabled = whole.disabled = sending;
    };
    // Both only for a picture that had an area (SPEC-318); the recipe says.
    let areaCheck = 0;
    const checkArea = async () => {
      const sequence = ++areaCheck;
      raw.hidden = whole.hidden = true;
      try {
        const recipe = await api(`/recipe/${current().entry.number}?chat_id=${encodeURIComponent(chatId)}`);
        if (sequence !== areaCheck || !details.open) return;
        raw.hidden = !recipe.area_png;
        whole.hidden = !recipe.area_png || !recipe.regenerable;
      } catch { /* No recipe: neither item applies. */ }
    };
    details.addEventListener('toggle', () => { if (details.open) { refresh(); if (!uploadsOnly) checkArea(); } });
    details.addEventListener('keydown', event => {
      if (event.key === 'Escape' && details.open) {
        event.preventDefault(); event.stopPropagation(); details.open = false; summary.focus({ preventScroll: true });
      }
    });
    return { element: details, refresh };
  }

  async function loadPreferences(chatId) {
    try {
      preferences.set(chatId, await api(`/preferences?chat_id=${encodeURIComponent(chatId)}`));
      renderPreferenceChip();
    } catch { /* The chip is informational; the menu reports load failures when opened. */ }
  }

  function renderPreferenceChip() {
    const existing = document.getElementById('pepito-editor-preference-chip');
    const data = preferences.get(currentChat());
    const composer = document.getElementById('message-input-container');
    let text = '';
    if (data && composer && !(data.auto_region && !data.editor_model_id && (!data.edit_method || data.edit_method === 'auto') && data.comparison_seed === null)) {
      const parts = [];
      const methodLabels = { qwen: 'Qwen', warp: 'Deformação', warp_qwen: 'Deformação + Qwen' };
      if (methodLabels[data.edit_method]) parts.push(`forma: ${methodLabels[data.edit_method]}`);
      if (data.editor_model_id) {
        const label = (data.editors || []).find(item => item.model_id === data.editor_model_id)?.label || data.editor_model_id;
        parts.push(data.editor_available === false ? `${label} (indisponível; selecione outro editor)` : label);
      }
      if (!data.auto_region) parts.push('região automática desativada');
      if (data.comparison_seed !== null) parts.push(`seed ${data.comparison_seed}`);
      text = `Próxima edição: ${parts.join(' · ')}`;
    }
    if (!text) { existing?.remove(); return; }
    if (existing && existing.textContent === text && composer.contains(existing)) return;
    existing?.remove();
    const label = node('div', 'pepito-preference-chip', text);
    label.id = 'pepito-editor-preference-chip';
    label.setAttribute('role', 'status');
    composer.prepend(label);
  }

  function buildGallery(mount) {
    const section = node('section', 'pepito-gallery');
    section.setAttribute('aria-label', 'Galeria de imagens Capella');
    const viewport = node('div', 'pepito-viewport');
    const open = button('', () => openViewer(mount, open), 'pepito-open-viewer');
    open.setAttribute('aria-label', 'Abrir imagem em tela cheia');
    const image = node('img', 'pepito-main-image');
    image.draggable = false;
    open.append(image);
    const count = node('span', 'pepito-counter');
    const previous = button('‹', () => step(-1), 'pepito-nav pepito-prev');
    previous.setAttribute('aria-label', 'Imagem anterior');
    const next = button('›', () => step(1), 'pepito-nav pepito-next');
    next.setAttribute('aria-label', 'Próxima imagem');
    const actions = node('div', 'pepito-gallery-overlay');
    const edit = button('Editar', () => openEditor(mount.chatId, mount.entries[mount.index].number, edit).catch(report), 'pepito-edit-image');
    const compareItem = { available: () => mount.canCompare(), run: () => mount.enterCompare() };
    const menu = imageActions(mount.chatId, () => ({ entry: mount.entries[mount.index], resource: mount.resource }), open, false, () => openViewer(mount, open, true), () => closeViewerFor(mount), compareItem);
    actions.append(edit, menu.element);
    const badge = node('span', 'pepito-badge', 'Preferida');
    viewport.append(open, count, previous, next, badge, actions);
    // Comparison mode (SPEC-319): two versions side by side, each with "Prefiro esta".
    const pair = node('div', 'pepito-compare');
    const cells = [0, 1].map(i => {
      const cell = node('div', 'pepito-compare-cell');
      const cellOpen = button('', () => { mount.index = i; openViewer(mount, cellOpen); }, 'pepito-compare-open');
      const cellImage = node('img', 'pepito-compare-image');
      cellImage.draggable = false;
      cellOpen.append(cellImage);
      const label = node('span', 'pepito-compare-label');
      const cellPrefer = button('Prefiro esta', () => mount.prefer(mount.entries[i].number), 'pepito-prefer-image pepito-primary');
      cell.append(cellOpen, label, cellPrefer);
      pair.append(cell);
      return { image: cellImage, open: cellOpen, label, prefer: cellPrefer };
    });
    const status = node('p', 'pepito-gallery-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    section.append(viewport, pair, status);
    // Hidden until the first image is decoded: Open WebUI's preview holds the place meanwhile.
    section.hidden = true;
    mount.section = section;
    mount.index = 0;
    mount.frame.after(section);
    // The viewport box is sized from the image's real dimensions, never from the <img>:
    // pausing or reloading a source must not collapse it (that shifted the chat's scroll).
    const sizes = new Map();
    const fitBox = ({ width, height }) => {
      viewport.style.aspectRatio = `${width} / ${height}`;
      viewport.style.width = `min(${width}px, 640px, calc(70vh * ${width} / ${height}))`;
    };
    mount.updateLabels = () => {
      const compare = mount.compare;
      section.classList.toggle('pepito-gallery-compare', compare);
      viewport.hidden = compare;
      pair.hidden = !compare;
      actions.hidden = compare;
      count.textContent = `${mount.index + 1} / ${mount.entries.length}`;
      count.hidden = previous.hidden = next.hidden = compare || mount.entries.length < 2;
      badge.hidden = compare || mount.entries[mount.index]?.number !== mount.preferred;
      cells.forEach((cell, i) => {
        const number = mount.entries[i]?.number;
        if (number === undefined) return;
        cell.label.textContent = `Imagem ${number}`;
        cell.open.setAttribute('aria-label', `Abrir imagem ${number} em tela cheia`);
        cell.prefer.setAttribute('aria-label', `Prefiro esta: imagem ${number}`);
        cell.open.disabled = cell.prefer.disabled = !(i === 0 ? mount.resource : mount.extra) || sending;
      });
      edit.disabled = open.disabled = !mount.resource || sending;
      menu.refresh();
    };
    mount.show = async (index) => {
      mount.index = (index + mount.entries.length) % mount.entries.length;
      const entry = mount.entries[mount.index];
      const sequence = mount.sequence = (mount.sequence || 0) + 1;
      const compare = mount.compare;
      menu.element.open = false;
      const known = sizes.get(entry.number);
      if (known) fitBox(known);
      releaseSource(mount.resource);
      releaseSource(mount.extra);
      mount.resource = mount.extra = null;
      // The box keeps its size; only the pixels disappear until the source is back.
      image.removeAttribute('src');
      for (const cell of cells) cell.image.removeAttribute('src');
      image.alt = `Imagem ${entry.number}`;
      section.setAttribute('aria-busy', 'true');
      status.textContent = compare ? 'Carregando imagens…' : 'Carregando imagem…';
      mount.updateLabels();
      try {
        if (compare) {
          const settled = await Promise.allSettled(mount.entries.map(item => acquireSource(mount.chatId, item.number)));
          const failed = settled.find(item => item.status === 'rejected');
          if (failed || sequence !== mount.sequence || !mount.frame.isConnected) {
            for (const item of settled) if (item.status === 'fulfilled') releaseSource(item.value);
            if (failed && sequence === mount.sequence) throw failed.reason;
            return;
          }
          [mount.resource, mount.extra] = settled.map(item => item.value);
          cells.forEach((cell, i) => {
            const resource = settled[i].value;
            sizes.set(mount.entries[i].number, { width: resource.width, height: resource.height });
            cell.image.style.aspectRatio = `${resource.width} / ${resource.height}`;
            cell.image.width = resource.width;
            cell.image.height = resource.height;
            cell.image.alt = `Imagem ${mount.entries[i].number}`;
            cell.image.src = resource.url;
          });
        } else {
          const resource = await acquireSource(mount.chatId, entry.number);
          if (sequence !== mount.sequence || !mount.frame.isConnected) { releaseSource(resource); return; }
          mount.resource = resource;
          sizes.set(entry.number, { width: resource.width, height: resource.height });
          fitBox(resource);
          image.width = resource.width;
          image.height = resource.height;
          image.src = resource.url;
        }
        status.textContent = '';
        mount.updateLabels();
      } catch (error) {
        if (sequence !== mount.sequence) return;
        status.replaceChildren(node('span', '', error.message), button('Tentar novamente', () => mount.show(mount.index).catch(report)));
        if (!mount.ready) throw error;
      } finally {
        if (sequence === mount.sequence) section.removeAttribute('aria-busy');
      }
    };
    mount.pause = () => {
      mount.sequence = (mount.sequence || 0) + 1;
      releaseSource(mount.resource);
      releaseSource(mount.extra);
      mount.resource = mount.extra = null;
      // Off-screen memory release only: the sized viewport stays, so no scroll shift.
      image.removeAttribute('src');
      for (const cell of cells) cell.image.removeAttribute('src');
      section.removeAttribute('aria-busy');
      mount.updateLabels();
    };
    const step = amount => mount.show(mount.index + amount).catch(report);
    const switchMode = async (compare, focusTarget) => {
      mount.compare = compare;
      status.textContent = '';
      await mount.show(0);
      focusTarget()?.focus({ preventScroll: true });
    };
    mount.canCompare = () => mount.entries.length === 2 && !mount.compare;
    mount.enterCompare = () => switchMode(true, () => cells[0].prefer);
    mount.prefer = async (number) => {
      status.textContent = '';
      try {
        const result = await prefer(mount.chatId, number);
        mount.preferred = result.preferred_number;
        mount.entries = arrangeEntries(mount.entries, mount.preferred);
        setSelection(mount.chatId, result.selected_number);
        await switchMode(false, () => open);
      } catch (error) { status.textContent = error.message; }
    };
    section.addEventListener('keydown', event => {
      if (mount.compare || menu.element.open || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      step(event.key === 'ArrowLeft' ? -1 : 1);
    });
    let swipe = null;
    open.addEventListener('pointerdown', event => {
      if (event.button === 0) swipe = { id: event.pointerId, x: event.clientX, y: event.clientY };
    });
    open.addEventListener('pointerup', event => {
      if (!swipe || swipe.id !== event.pointerId) return;
      const dx = event.clientX - swipe.x;
      if (Math.abs(dx) > 45 && Math.abs(event.clientY - swipe.y) < 80) {
        step(dx < 0 ? 1 : -1);
        open.dataset.swiped = 'true';
      }
      swipe = null;
    });
    open.addEventListener('click', event => {
      if (open.dataset.swiped) { event.stopImmediatePropagation(); delete open.dataset.swiped; }
    }, true);
    open.addEventListener('pointercancel', () => { swipe = null; });
  }

  /* What the viewer shows: a Pepito gallery (numbered sources, full menu) or pictures the
   * user attached (their own files: zoom, drag and Download; nothing to select or approve). */
  function galleryAlbum(mount) {
    return {
      mount, chatId: mount.chatId, uploads: false,
      count: () => mount.entries.length,
      entry: index => mount.entries[index],
      alt: index => `Imagem ${mount.entries[index].number}`,
      acquire: index => acquireSource(mount.chatId, mount.entries[index].number),
      release: releaseSource
    };
  }

  function uploadAlbum(urls) {
    return {
      mount: null, chatId: currentChat(), uploads: true,
      count: () => urls.length,
      entry: () => ({}),
      alt: index => urls.length > 1 ? `Imagem enviada ${index + 1}` : 'Imagem enviada',
      name: (index, resource) => {
        const type = /^image\/([a-z0-9]+)/i.exec(resource?.type || '')?.[1].toLowerCase();
        const extension = type === 'jpeg' ? 'jpg' : type;
        return `imagem-enviada${urls.length > 1 ? `-${index + 1}` : ''}${extension ? `.${extension}` : ''}`;
      },
      acquire: index => loadUpload(urls[index]),
      release: resource => { if (resource?.owned) URL.revokeObjectURL(resource.url); }
    };
  }

  // What the model drew before the area was pasted on the source (SPEC-318): one picture,
  // Download only, with a label that says it is not the delivered picture.
  async function loadRaw(chatId, number) {
    const response = await fetchNative(`${API}/raw/${number}?chat_id=${encodeURIComponent(chatId)}`, {
      headers: authHeaders(), credentials: 'same-origin', redirect: 'error'
    });
    const blob = await limitedBlob(response, MAX_IMAGE_BYTES);
    if (!/^image\//i.test(blob.type)) throw new Error('A saída do modelo não é uma imagem válida.');
    const resource = {
      url: URL.createObjectURL(blob), owned: true, type: blob.type, width: 0, height: 0,
      crop: response.headers.get('X-Pepito-Raw-Crop') === '1'
    };
    try {
      const image = new Image();
      image.src = resource.url;
      await image.decode();
      resource.width = image.naturalWidth;
      resource.height = image.naturalHeight;
      if (!resource.width || !resource.height) throw new Error('empty');
      return resource;
    } catch {
      URL.revokeObjectURL(resource.url);
      throw new Error('Não foi possível abrir a saída do modelo.');
    }
  }

  function rawAlbum(chatId, number, resource) {
    return {
      mount: null, chatId, uploads: true,
      count: () => 1,
      entry: () => ({ number }),
      alt: () => `Saída do modelo da imagem ${number}, antes da colagem`,
      label: shown => shown?.crop ? 'Saída do modelo (só o recorte)' : 'Saída do modelo, antes da colagem',
      name: () => `imagem-${number}-saida-do-modelo.png`,
      acquire: async () => resource,
      release: shown => { if (shown?.owned) URL.revokeObjectURL(shown.url); }
    };
  }

  // Opens it over whatever viewer is open, without sending anything to the chat.
  async function showRaw(chatId, number, resource, opener) {
    if (viewer) {
      const old = viewer.dialog;
      await new Promise(resolve => { old.addEventListener('close', resolve, { once: true }); old.close(); });
    }
    openAlbum(rawAlbum(chatId, number, resource), 0, opener);
  }

  // Same-origin files and composer data: URLs are read into a blob, so Download keeps the
  // real format; any other address is shown exactly as the page already shows it.
  async function loadUpload(url) {
    let blob = null;
    const local = canonical(url);
    if (local || /^data:image\//i.test(url)) {
      try {
        const token = local ? localStorage.getItem('token') : '';
        const response = await fetchNative(local || url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {}, credentials: 'same-origin', redirect: 'error'
        });
        const body = response.ok ? await limitedBlob(response, MAX_IMAGE_BYTES) : null;
        if (body && /^image\//i.test(body.type)) blob = body;
      } catch { /* The browser can still display the address the message already shows. */ }
    }
    const resource = { url: blob ? URL.createObjectURL(blob) : url, owned: !!blob, type: blob?.type || '', width: 0, height: 0 };
    try {
      const image = new Image();
      image.src = resource.url;
      await image.decode();
      resource.width = image.naturalWidth;
      resource.height = image.naturalHeight;
      if (!resource.width || !resource.height) throw new Error('empty');
      return resource;
    } catch {
      if (resource.owned) URL.revokeObjectURL(resource.url);
      throw new Error('Não foi possível abrir a imagem enviada.');
    }
  }

  function openViewer(mount, opener, withPrompt = false) {
    openAlbum(galleryAlbum(mount), mount.index, opener, withPrompt);
  }

  function openAlbum(album, start, opener, withPrompt = false) {
    if (viewer) { viewer.dialog.focus(); return; }
    closeBracketHint();
    const mount = album.mount;
    const dialog = node('dialog', 'pepito-viewer-dialog');
    dialog.setAttribute('aria-label', 'Visualizador de imagem em tela cheia');
    const state = { dialog, mount, chatId: album.chatId, resource: null, index: start, sequence: 0 };
    viewer = state;
    const shell = node('div', 'pepito-viewer');
    const toolbar = node('div', 'pepito-viewer-toolbar');
    const close = button('×', () => dialog.close(), 'pepito-viewer-close');
    close.setAttribute('aria-label', 'Fechar visualizador');
    const zoomOut = button('−', () => setZoom(zoom / 1.25));
    zoomOut.setAttribute('aria-label', 'Diminuir zoom');
    const percent = node('output', 'pepito-viewer-percent', '100%');
    percent.setAttribute('aria-live', 'polite');
    const zoomIn = button('+', () => setZoom(zoom * 1.25));
    zoomIn.setAttribute('aria-label', 'Aumentar zoom');
    const fitButton = button('Ajustar', fit, 'pepito-viewer-fit');
    const edit = button('Editar', () => openEditor(state.chatId, album.entry(state.index).number, edit).catch(report), 'pepito-edit-image');
    const menu = imageActions(state.chatId, () => ({
      entry: album.entry(state.index), resource: state.resource, name: album.name?.(state.index, state.resource)
    }), album.uploads ? close : edit, album.uploads, album.uploads ? null : () => togglePrompt(true), () => dialog.close());
    toolbar.append(zoomOut, percent, zoomIn, fitButton, ...(album.uploads ? [] : [edit]), menu.element, close);
    const stage = node('div', 'pepito-viewer-stage');
    stage.tabIndex = 0;
    stage.setAttribute('aria-label', 'Imagem ampliada. Arraste para mover; duplo clique recentraliza; use a roda do mouse para zoom, ou +, − e Ajustar.');
    const center = node('div', 'pepito-viewer-center');
    const image = node('img', 'pepito-viewer-image');
    image.draggable = false;
    center.append(image);
    stage.append(center);
    const previous = button('‹', () => show(state.index - 1), 'pepito-viewer-prev');
    previous.setAttribute('aria-label', 'Imagem anterior');
    const next = button('›', () => show(state.index + 1), 'pepito-viewer-next');
    next.setAttribute('aria-label', 'Próxima imagem');
    const count = node('span', 'pepito-viewer-counter');
    count.hidden = previous.hidden = next.hidden = album.count() < 2;
    const status = node('p', 'pepito-viewer-status');
    status.setAttribute('role', 'status');
    const body = node('div', 'pepito-viewer-body');
    const promptPanel = node('aside', 'pepito-prompt-panel');
    promptPanel.setAttribute('aria-label', 'Prompt da imagem');
    promptPanel.hidden = true;
    body.append(stage, promptPanel);
    const label = node('p', 'pepito-viewer-label');
    label.setAttribute('role', 'note');
    shell.append(toolbar, label, body, previous, next, count, status);
    dialog.append(shell);
    document.body.append(dialog);
    let zoom = 1;
    let fitted = true;
    let pan = null;
    // The picture is centered on the stage and moved by `offset` (screen px), never by
    // scrolling: it can be dragged anywhere at any zoom, small or large.
    const offset = { x: 0, y: 0 };
    function place() {
      if (!state.resource) return;
      // A strip of the picture always stays on the stage, so it can never be lost.
      const halfX = (stage.clientWidth + state.resource.width * zoom) / 2;
      const halfY = (stage.clientHeight + state.resource.height * zoom) / 2;
      const keepX = Math.min(48, state.resource.width * zoom);
      const keepY = Math.min(48, state.resource.height * zoom);
      offset.x = Math.max(keepX - halfX, Math.min(halfX - keepX, offset.x));
      offset.y = Math.max(keepY - halfY, Math.min(halfY - keepY, offset.y));
      image.style.translate = offset.x || offset.y ? `${offset.x}px ${offset.y}px` : '';
    }
    function setZoom(value, fitMode = false, anchor = null) {
      if (!state.resource) return;
      const old = zoom;
      zoom = Math.max(0.02, Math.min(8, value));
      fitted = fitMode;
      image.style.width = `${state.resource.width * zoom}px`;
      image.style.height = `${state.resource.height * zoom}px`;
      percent.textContent = `${Math.round(zoom * 100)}%`;
      if (fitMode) { offset.x = offset.y = 0; place(); return; }
      // The point that stays still: the pointer for the wheel, the stage's center otherwise
      // (both measured from the stage's center, where the picture's center sits at `offset`).
      const ax = anchor ? anchor.x - stage.clientWidth / 2 : 0;
      const ay = anchor ? anchor.y - stage.clientHeight / 2 : 0;
      offset.x = ax - (ax - offset.x) * zoom / old;
      offset.y = ay - (ay - offset.y) * zoom / old;
      place();
    }
    function fit() {
      if (!state.resource) return;
      setZoom(Math.min((stage.clientWidth - 32) / state.resource.width, (stage.clientHeight - 32) / state.resource.height, 1), true);
    }
    async function show(index) {
      state.index = (index + album.count()) % album.count();
      const sequence = ++state.sequence;
      menu.element.open = false;
      album.release(state.resource);
      state.resource = null;
      image.removeAttribute('src');
      edit.disabled = zoomIn.disabled = zoomOut.disabled = fitButton.disabled = true;
      menu.refresh();
      count.textContent = `${state.index + 1} / ${album.count()}`;
      if (!promptPanel.hidden) loadPrompt();
      status.textContent = 'Carregando imagem…';
      try {
        const resource = await album.acquire(state.index);
        if (viewer !== state || sequence !== state.sequence) { album.release(resource); return; }
        state.resource = resource;
        image.src = resource.url;
        image.alt = album.alt(state.index);
        label.textContent = album.label?.(resource) || '';
        edit.disabled = zoomIn.disabled = zoomOut.disabled = fitButton.disabled = false;
        menu.refresh();
        status.textContent = '';
        fit();
      } catch (error) {
        if (viewer === state && sequence === state.sequence) {
          status.replaceChildren(node('span', '', error.message), button('Tentar novamente', () => show(state.index)));
        }
      }
    }
    let promptSequence = 0;
    function togglePrompt(open) {
      promptPanel.hidden = !open;
      shell.classList.toggle('pepito-prompt-open', open);
      if (open) loadPrompt();
      if (fitted) requestAnimationFrame(fit);
    }
    async function loadPrompt() {
      const entry = album.entry(state.index);
      const sequence = ++promptSequence;
      const closeButton = button('×', () => togglePrompt(false), 'pepito-prompt-close');
      closeButton.setAttribute('aria-label', 'Fechar prompt');
      const header = node('div', 'pepito-prompt-header');
      header.append(node('h2', 'pepito-prompt-title', `Prompt da imagem ${entry.number}`), closeButton);
      promptPanel.replaceChildren(header, node('p', 'pepito-prompt-status', 'Carregando a receita…'));
      try {
        const recipe = await api(`/recipe/${entry.number}?chat_id=${encodeURIComponent(state.chatId)}`);
        if (viewer !== state || sequence !== promptSequence) return;
        promptPanel.replaceChildren(header, ...promptView(state, recipe, () => dialog.close()));
      } catch (error) {
        if (viewer === state && sequence === promptSequence) {
          promptPanel.replaceChildren(header, node('p', 'pepito-prompt-status', error.message));
        }
      }
    }
    // The mouse wheel zooms around the pointer (the page behind never scrolls).
    stage.addEventListener('wheel', event => {
      if (!state.resource) return;
      event.preventDefault();
      const box = stage.getBoundingClientRect();
      // Lines (Firefox, some mice) and pages become pixels: 1 line ≈ 33 px, 1 page = the stage's height.
      const pixels = event.deltaY * (event.deltaMode === 1 ? 33 : event.deltaMode === 2 ? stage.clientHeight : 1);
      const step = Math.pow(1.0015, -Math.max(-240, Math.min(240, pixels)));
      setZoom(zoom * step, false, { x: event.clientX - box.left, y: event.clientY - box.top });
    }, { passive: false });
    stage.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      pan = { id: event.pointerId, x: event.clientX, y: event.clientY };
      stage.setPointerCapture(event.pointerId);
      stage.classList.add('pepito-panning');
    });
    stage.addEventListener('pointermove', event => {
      if (pan?.id !== event.pointerId || !state.resource) return;
      offset.x += event.clientX - pan.x;
      offset.y += event.clientY - pan.y;
      pan.x = event.clientX;
      pan.y = event.clientY;
      // A dragged picture stays where it was put when the window resizes.
      fitted = false;
      place();
    });
    const endPan = () => { pan = null; stage.classList.remove('pepito-panning'); };
    stage.addEventListener('pointerup', endPan);
    stage.addEventListener('pointercancel', endPan);
    stage.addEventListener('lostpointercapture', endPan);
    stage.addEventListener('dblclick', event => { event.preventDefault(); fit(); });
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Tab') {
        const focusable = [...dialog.querySelectorAll('button:not(:disabled),summary,input,select,textarea,[tabindex="0"]')]
          .filter(element => element.getClientRects().length && !element.closest('details:not([open]) .pepito-menu-panel'));
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
      if (event.target.closest('input,select,textarea,details[open]')) return;
      if (['ArrowLeft', 'ArrowRight', '+', '=', '-', '0'].includes(event.key)) {
        event.preventDefault();
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') show(state.index + (event.key === 'ArrowLeft' ? -1 : 1));
        else if (event.key === '0') fit();
        else setZoom(event.key === '-' ? zoom / 1.25 : zoom * 1.25);
      }
    });
    const resize = new ResizeObserver(() => { if (fitted) fit(); else place(); });
    resize.observe(stage);
    dialog.addEventListener('close', () => {
      state.sequence += 1;
      resize.disconnect();
      image.removeAttribute('src');
      album.release(state.resource);
      state.resource = null;
      dialog.remove();
      if (viewer === state) viewer = null;
      const restoreFocus = () => {
        if (viewer || editor || state.sent || currentChat() !== state.chatId) return;
        if (document.activeElement !== document.body && document.activeElement !== opener) return;
        const target = opener?.isConnected ? opener : mount?.section?.querySelector('.pepito-open-viewer');
        if (target && !target.disabled) target.focus({ preventScroll: true });
      };
      // Synchronising the displayed image temporarily disables the opener and drops its focus.
      if (mount?.section?.isConnected && mount.index !== state.index) {
        mount.show(state.index).then(restoreFocus).catch(report);
      } else restoreFocus();
    }, { once: true });
    dialog.showModal();
    close.focus();
    show(state.index);
    if (withPrompt) togglePrompt(true);
  }

  const PART_LABELS = {
    generate: 'Geração', generate_hq: 'Geração em alta qualidade', edit: 'Edição',
    restage: 'Reencenação', pose: 'Pose', remove: 'Remoção', swap: 'Troca de personagem'
  };
  const MODE_LABELS = { local: 'local', redraw: 'redesenho', restage: 'reencenação', swap: 'troca', remove: 'remoção' };
  const STEP_LABELS = {
    prompt: 'Prompt', reference: 'Referência limpa da personagem',
    marked: 'Variação 2: Qwen com marcação', scene: 'Variação 1: Krea'
  };

  // The recipe of one picture as the Prompt panel shows it, with its three Regerar actions.
  function promptView(state, recipe, closeViewer) {
    const facts = node('dl', 'pepito-prompt-facts');
    const fact = (label, value) => {
      const term = node('dt', '', label);
      const detail = node('dd');
      if (value instanceof Node) detail.append(value); else detail.textContent = value;
      facts.append(term, detail);
    };
    const modeLabel = recipe.edit_mode && MODE_LABELS[recipe.edit_mode] && recipe.part !== recipe.edit_mode ? ` · ${MODE_LABELS[recipe.edit_mode]}` : '';
    fact('Parte', `${PART_LABELS[recipe.part] || recipe.part || '—'}${modeLabel}`);
    fact('Origem', recipe.sources.length ? `Imagem ${recipe.sources.join(', ')}` : recipe.kind === 'generate' ? 'Só o texto' : '—');
    if (recipe.area_png) {
      const area = node('img', 'pepito-prompt-area');
      area.src = recipe.area_png;
      area.alt = 'Área usada nesta imagem';
      fact('Área', area);
    } else fact('Área', 'Nenhuma');
    fact('Canvas', recipe.canvas ? `${recipe.canvas.width} × ${recipe.canvas.height}` : '—');
    fact('Semente', recipe.seed === null || recipe.seed === undefined ? '—' : String(recipe.seed));
    const parts = [facts];
    const steps = new Map(recipe.steps.map(item => [item.key, item.prompt]));
    let step = recipe.step;
    const field = node('label', 'pepito-prompt-field', 'Texto do prompt');
    const text = node('textarea', 'pepito-prompt-text');
    text.rows = 9; text.maxLength = 20000; text.spellcheck = false;
    text.value = steps.get(step) || '';
    field.append(text);
    if (steps.size > 1) {
      // A swap has a prompt per step: the character's clean reference and this variation's.
      const label = node('label', 'pepito-prompt-field', 'Etapa');
      const select = node('select', 'pepito-prompt-step');
      for (const key of steps.keys()) {
        const option = node('option', '', STEP_LABELS[key] || key);
        option.value = key;
        select.append(option);
      }
      select.value = step;
      select.addEventListener('change', () => { step = select.value; text.value = steps.get(step) || ''; });
      label.append(select);
      parts.push(label);
    }
    parts.push(field);
    const status = node('p', 'pepito-prompt-status');
    status.setAttribute('role', 'status');
    const actions = node('div', 'pepito-prompt-actions');
    const run = mode => async () => {
      edited.disabled = same.disabled = seeded.disabled = restore.disabled = true;
      status.textContent = '';
      try {
        if (mode === 'edited' && !text.value.trim()) throw new Error('Escreva o prompt antes de regerar.');
        await sendRegeneration(state, {
          chat_id: state.chatId, number: recipe.number, mode, step,
          ...(mode === 'edited' ? { prompt: text.value } : {})
        }, closeViewer);
      } catch (error) { status.textContent = error.message; }
      finally {
        edited.disabled = same.disabled = restore.disabled = !recipe.regenerable || sending;
        seeded.disabled = !recipe.regenerable || !hasSeed || sending;
      }
    };
    const hasSeed = recipe.seed !== null && recipe.seed !== undefined;
    const edited = button('Regerar com este prompt', run('edited'), 'pepito-primary');
    const seeded = button('Regerar (mesma semente)', run('seed'));
    const same = button('Regerar (nova semente)', run('same'));
    const restore = button('Restaurar texto', () => { text.value = steps.get(step) || ''; text.focus(); });
    actions.append(edited, seeded, same, restore);
    parts.push(actions, node('p', 'pepito-helper', 'Regerar envia uma mensagem fixa e faz uma imagem nova com esta receita, sem passar pelo roteador. Com o prompt editado a semente é a mesma. Mesma semente repete o prompt gravado: com o mesmo modelo, a imagem tende a sair igual, o que serve para conferir uma mudança de modelo ou de configuração. Nova semente sorteia outra variação.'), status);
    if (!recipe.regenerable) {
      edited.disabled = same.disabled = seeded.disabled = true;
      status.textContent = 'Esta receita não pode ser refeita: uma origem dela não está mais no chat.';
    } else if (!hasSeed) {
      seeded.disabled = true;
      status.textContent = NO_SEED;
    }
    return parts;
  }

  // Upstream's send button and editor, exactly as the user would type the fixed message.
  async function typeAndSend(text) {
    restoreComposer(text);
    const same = value => value.replace(/\s+/g, ' ').trim() === text;
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 100));
      if (!same(composerText())) continue;
      const send = document.getElementById('send-message-button');
      if (send && !send.disabled) { send.click(); return true; }
    }
    return false;
  }

  // Like an attached area, the recipe is armed on the server for one fixed message and
  // expires; the filter uses it up and runs it without the router.
  async function sendRegeneration(state, payload, closeViewer) {
    if (sending) throw new Error('Aguarde o envio terminar antes de regerar.');
    if (currentChat() !== state.chatId) throw new Error('Volte à conversa da imagem para regerar.');
    if (draft) throw new Error('Remova a área anexada antes de regerar: ela vale para outro pedido.');
    if (composerText()) throw new Error('Esvazie o campo de mensagem antes de regerar: o pedido é enviado por ele.');
    const armed = await api('/regenerate', { method: 'POST', body: JSON.stringify(payload) });
    state.sent = true;
    closeViewer();
    // The dialog is modal: the composer can take the text only once it is closed.
    await new Promise(resolve => setTimeout(resolve, 0));
    if (await typeAndSend(armed.text)) announce(`Regerando a imagem ${armed.number}.`);
    else announce(`Não consegui enviar sozinho. Envie “${armed.text}” em até 5 minutos.`, true);
  }

  // Pictures the user attached (in sent messages, or still in the composer) open in this
  // viewer instead of Open WebUI's. Open WebUI 0.12.0 renders each one through its Image
  // component: <button><img data-cy="image"></button>; this capture listener runs before
  // that button's own click handler.
  const ATTACHED_IMAGE = 'button > img[data-cy="image"]';
  document.addEventListener('click', event => {
    if (event.button !== 0 || event.defaultPrevented) return;
    const trigger = event.target?.closest?.('button');
    const picture = trigger?.querySelector(':scope > img[data-cy="image"]');
    const scope = picture && (trigger.closest('.user-message') || trigger.closest('#message-input-container'));
    if (!scope || !picture.getAttribute('src')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    // A picture this chat already numbered in a gallery opens there, with the full menu.
    const address = canonical(picture.getAttribute('src'));
    const chatId = currentChat();
    for (const mount of mounts) {
      if (!address || !chatId || mount.chatId !== chatId || !mount.ready || !mount.section?.isConnected) continue;
      const index = mount.entries.findIndex(entry => canonical(entry.url) === address);
      if (index >= 0) { openAlbum(galleryAlbum(mount), index, trigger); return; }
    }
    const pictures = [...scope.querySelectorAll(ATTACHED_IMAGE)].filter(item => item.getAttribute('src'));
    openAlbum(uploadAlbum(pictures.map(item => item.getAttribute('src'))), Math.max(0, pictures.indexOf(picture)), trigger);
  }, true);

  function renderChip() {
    chip?.remove();
    chip = null;
    if (!draft || draft.chatId !== currentChat()) return;
    const composer = document.getElementById('message-input-container');
    if (!composer) return;
    chip = node('div', 'pepito-mask-chip');
    chip.setAttribute('role', 'group');
    chip.setAttribute('aria-label', `Área anexada da imagem ${draft.number}`);
    const thumbnail = node('img');
    thumbnail.src = draft.resource.url;
    thumbnail.alt = '';
    const edit = button('', () => openEditor(draft.chatId, draft.number, edit).catch(report), 'pepito-chip-edit');
    edit.append(node('strong', '', 'Área selecionada'), node('span', '', `Imagem ${draft.number} · ajustar seleção`));
    const remove = button('×', () => {
      if (sending) return;
      clearDraft();
      announce('Área removida. A instrução no campo de texto não foi alterada.');
    }, 'pepito-chip-remove');
    remove.setAttribute('aria-label', 'Remover área selecionada');
    edit.disabled = remove.disabled = sending;
    chip.append(thumbnail, edit, remove);
    // Never place this inside #chat-input / the contenteditable document.
    composer.prepend(chip);
  }

  function clearDraft() {
    if (draft) releaseSource(draft.resource);
    draft = null;
    renderChip();
  }

  function canvasBlob(canvas) {
    return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Não foi possível preparar a área.')), 'image/png'));
  }

  function dataURI(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('Não foi possível preparar o PNG da área.'));
      reader.readAsDataURL(blob);
    });
  }

  async function openEditor(chatId, number, opener) {
    if (sending) throw new Error('Aguarde o envio terminar antes de editar a área.');
    if (editor) { editor.dialog?.focus(); return; }
    if (currentChat() !== chatId) throw new Error('Volte à conversa da imagem para editar a área.');
    const state = { chatId, number, opener, resource: null, dialog: null, closed: false };
    // Reserve synchronously: repeated clicks cannot open duplicate dialogs.
    editor = state;
    try {
      state.resource = await acquireSource(chatId, number);
      if (state.closed || currentChat() !== chatId) throw new Error('A conversa mudou. Abra o editor novamente.');
      buildEditor(state);
    } catch (error) {
      releaseSource(state.resource);
      if (editor === state) editor = null;
      throw error;
    }
  }

  function buildEditor(state) {
    const resource = state.resource;
    const dialog = node('dialog', 'pepito-editor-dialog');
    state.dialog = dialog;
    dialog.setAttribute('aria-labelledby', 'pepito-editor-title');
    dialog.setAttribute('aria-describedby', 'pepito-editor-help');
    const shell = node('div', 'pepito-editor');
    const head = node('header', 'pepito-editor-head');
    const heading = node('div');
    const title = node('h2', '', `Editar área · imagem ${state.number}`);
    title.id = 'pepito-editor-title';
    const help = node('p', '', 'Pinte o que deve mudar. Fora da seleção, preservar a imagem original.');
    help.id = 'pepito-editor-help';
    heading.append(title, help);
    const close = button('×', () => dialog.close());
    close.setAttribute('aria-label', 'Fechar editor sem aplicar');
    head.append(heading, close);
    const body = node('div', 'pepito-editor-body');
    const stage = node('div', 'pepito-editor-stage');
    const center = node('div', 'pepito-stage-center');
    const surface = node('div', 'pepito-editor-surface');
    const image = node('img');
    image.src = resource.url;
    image.alt = `Original autorizado da imagem ${state.number}`;
    image.draggable = false;
    const mask = node('canvas', 'pepito-mask-canvas');
    mask.width = resource.width;
    mask.height = resource.height;
    mask.tabIndex = 0;
    mask.setAttribute('role', 'img');
    mask.setAttribute('aria-label', 'Seleção da área. Arraste para pintar. Pelo teclado, use setas para mover e Espaço para pintar; Shift e setas pintam um traço.');
    const cursor = node('span', 'pepito-brush-cursor');
    cursor.hidden = true;
    surface.append(image, mask, cursor);
    center.append(surface);
    stage.append(center);
    const tools = node('div', 'pepito-editor-tools');
    const toolLabel = node('span', 'pepito-tool-label', 'Ferramenta');
    const toolSwitch = node('div', 'pepito-tool-switch');
    const brush = button('Pincel', () => setTool(false));
    const eraser = button('Borracha', () => setTool(true));
    toolSwitch.append(brush, eraser);
    const sizeLabel = node('label', 'pepito-tool-label', 'Tamanho do pincel');
    const size = node('input');
    size.type = 'range'; size.min = '2'; size.max = '256'; size.value = '48';
    size.id = 'pepito-brush-size'; sizeLabel.htmlFor = size.id;
    const sizeValue = node('output', '', '48 px');
    sizeValue.htmlFor = size.id;
    const undo = button('Desfazer', undoStroke);
    const clear = button('Limpar área', () => { finishStroke(); history.push({ clear: true }); trimHistory(); redraw(); controls(); });
    const zoomLabel = node('span', 'pepito-tool-label', 'Zoom');
    const zoomRow = node('div', 'pepito-zoom-row');
    const zoomOut = button('−', () => setZoom(zoom / 1.25));
    zoomOut.setAttribute('aria-label', 'Diminuir zoom');
    const zoomValue = node('output', '', '100%');
    zoomValue.setAttribute('aria-live', 'polite');
    const zoomIn = button('+', () => setZoom(zoom * 1.25));
    zoomIn.setAttribute('aria-label', 'Aumentar zoom');
    zoomRow.append(zoomOut, zoomValue, zoomIn);
    const fitButton = button('Ajustar à janela', fit);
    const keyboardHelp = node('p', 'pepito-helper', 'Arraste para pintar; role para deslocar. Use o zoom para detalhes. No canvas: setas movem, Espaço pinta, Shift + setas pinta um traço. Ctrl/⌘ + Z desfaz.');
    tools.append(toolLabel, toolSwitch, sizeLabel, size, sizeValue, undo, clear, zoomLabel, zoomRow, fitButton, keyboardHelp);
    body.append(stage, tools);
    const foot = node('footer', 'pepito-editor-foot');
    const status = node('p', 'pepito-editor-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const footActions = node('div', 'pepito-actions');
    const cancel = button('Cancelar', () => dialog.close());
    const apply = button('Anexar área ao pedido', applyArea, 'pepito-primary');
    footActions.append(cancel, apply);
    foot.append(status, footActions);
    shell.append(head, body, foot);
    dialog.append(shell);
    document.body.append(dialog);

    const context = mask.getContext('2d', { willReadFrequently: true });
    const base = document.createElement('canvas');
    base.width = mask.width; base.height = mask.height;
    const baseContext = base.getContext('2d');
    const history = [];
    let current = null;
    let pointerId = null;
    let erasing = false;
    let zoom = 1;
    let working = false;
    let keyboardPoint = { x: mask.width / 2, y: mask.height / 2 };

    function drawStroke(ctx, stroke) {
      if (stroke.clear) { ctx.clearRect(0, 0, mask.width, mask.height); return; }
      ctx.globalCompositeOperation = stroke.erase ? 'destination-out' : 'source-over';
      ctx.strokeStyle = '#ff527e'; ctx.fillStyle = '#ff527e';
      ctx.lineWidth = stroke.size; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.beginPath();
      const points = stroke.points;
      ctx.moveTo(points[0][0], points[0][1]);
      if (points.length === 1) {
        ctx.arc(points[0][0], points[0][1], stroke.size / 2, 0, Math.PI * 2);
        ctx.fill();
      } else {
        for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i][0], points[i][1]);
        ctx.stroke();
      }
      ctx.globalCompositeOperation = 'source-over';
    }

    function trimHistory() {
      let points = history.reduce((sum, stroke) => sum + (stroke.points?.length || 0), 0);
      while (history.length > MAX_UNDO || points > MAX_POINTS) {
        const oldest = history.shift();
        drawStroke(baseContext, oldest);
        points -= oldest.points?.length || 0;
      }
    }

    function redraw() {
      context.clearRect(0, 0, mask.width, mask.height);
      context.drawImage(base, 0, 0);
      for (const stroke of history) drawStroke(context, stroke);
    }

    function controls() {
      undo.disabled = working || !history.length;
      clear.disabled = apply.disabled = working;
      brush.disabled = eraser.disabled = size.disabled = working;
      close.disabled = cancel.disabled = working;
      brush.setAttribute('aria-pressed', String(!erasing));
      eraser.setAttribute('aria-pressed', String(erasing));
    }

    function setTool(erase) { finishStroke(); erasing = erase; controls(); }
    function updateCursor() {
      cursor.style.left = `${keyboardPoint.x / mask.width * 100}%`;
      cursor.style.top = `${keyboardPoint.y / mask.height * 100}%`;
      cursor.style.width = cursor.style.height = `${Number(size.value) * zoom}px`;
    }
    size.addEventListener('input', () => { sizeValue.textContent = `${size.value} px`; updateCursor(); });

    function setZoom(value) {
      const old = zoom;
      zoom = Math.max(0.02, Math.min(4, value));
      const x = (stage.scrollLeft + stage.clientWidth / 2) / old;
      const y = (stage.scrollTop + stage.clientHeight / 2) / old;
      surface.style.width = `${mask.width * zoom}px`;
      surface.style.height = `${mask.height * zoom}px`;
      zoomValue.textContent = `${Math.round(zoom * 100)}%`;
      stage.scrollLeft = x * zoom - stage.clientWidth / 2;
      stage.scrollTop = y * zoom - stage.clientHeight / 2;
      updateCursor();
    }

    function fit() {
      setZoom(Math.min((stage.clientWidth - 40) / mask.width, (stage.clientHeight - 40) / mask.height, 1));
      stage.scrollLeft = stage.scrollTop = 0;
    }

    function point(event) {
      const rect = mask.getBoundingClientRect();
      return [Math.max(0, Math.min(mask.width, (event.clientX - rect.left) * mask.width / rect.width)),
        Math.max(0, Math.min(mask.height, (event.clientY - rect.top) * mask.height / rect.height))];
    }

    function startStroke(p) {
      current = { erase: erasing, size: Number(size.value), points: [p] };
      drawStroke(context, current);
    }

    function continueStroke(p) {
      if (!current) return;
      const last = current.points[current.points.length - 1];
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 0.5) return;
      drawStroke(context, { ...current, points: [last, p] });
      current.points.push(p);
      if (current.points.length >= 2048) { finishStroke(); startStroke(p); }
    }

    function finishStroke() {
      if (!current) return;
      history.push(current);
      current = null;
      trimHistory();
      controls();
    }

    function undoStroke() { if (working) return; finishStroke(); history.pop(); redraw(); controls(); }
    mask.addEventListener('pointerdown', event => {
      if (working || event.button !== 0 || pointerId !== null) return;
      event.preventDefault();
      mask.focus({ preventScroll: true });
      cursor.hidden = true;
      pointerId = event.pointerId;
      mask.setPointerCapture(event.pointerId);
      startStroke(point(event));
    });
    mask.addEventListener('pointermove', event => {
      if (pointerId === event.pointerId) continueStroke(point(event));
    });
    const endPointer = event => { if (pointerId === event.pointerId) { finishStroke(); pointerId = null; } };
    mask.addEventListener('pointerup', endPointer);
    mask.addEventListener('pointercancel', endPointer);
    mask.addEventListener('lostpointercapture', endPointer);
    mask.addEventListener('keydown', event => {
      if (working) return;
      const moves = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      if (moves[event.key]) {
        event.preventDefault();
        const before = [keyboardPoint.x, keyboardPoint.y];
        const step = Math.max(1, Number(size.value) / 4);
        keyboardPoint = {
          x: Math.max(0, Math.min(mask.width, keyboardPoint.x + moves[event.key][0] * step)),
          y: Math.max(0, Math.min(mask.height, keyboardPoint.y + moves[event.key][1] * step))
        };
        cursor.hidden = false;
        updateCursor();
        if (event.shiftKey) {
          if (!current) startStroke(before);
          continueStroke([keyboardPoint.x, keyboardPoint.y]);
        } else finishStroke();
      } else if (event.code === 'Space') {
        event.preventDefault();
        if (!event.repeat) { startStroke([keyboardPoint.x, keyboardPoint.y]); finishStroke(); }
      }
    });
    mask.addEventListener('keyup', event => { if (event.key === 'Shift' || event.key.startsWith('Arrow')) finishStroke(); });
    mask.addEventListener('blur', () => { finishStroke(); cursor.hidden = true; });
    dialog.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault(); undoStroke();
      }
    });
    dialog.addEventListener('cancel', event => { if (working) event.preventDefault(); });

    async function applyArea() {
      if (working) return;
      finishStroke();
      working = true;
      controls();
      status.textContent = 'Preparando área…';
      const output = document.createElement('canvas');
      output.width = mask.width; output.height = mask.height;
      try {
        // Bounded row buffers; output is strictly opaque white-selected/black-unselected.
        const outputContext = output.getContext('2d');
        let selected = false;
        for (let y = 0; y < mask.height; y += 128) {
          const pixels = context.getImageData(0, y, mask.width, Math.min(128, mask.height - y));
          for (let i = 0; i < pixels.data.length; i += 4) {
            const value = pixels.data[i + 3] >= 128 ? 255 : 0;
            if (value) selected = true;
            pixels.data[i] = pixels.data[i + 1] = pixels.data[i + 2] = value;
            pixels.data[i + 3] = 255;
          }
          outputContext.putImageData(pixels, 0, y);
        }
        if (!selected) throw new Error('Pinte uma área com o pincel antes de anexar.');
        const png = await canvasBlob(output);
        if (png.size > MAX_MASK_BYTES) throw new Error('A seleção excede 8 MiB. Simplifique a área pintada e tente novamente.');
        const pngURI = await dataURI(png);
        if (currentChat() !== state.chatId) throw new Error('A conversa mudou. Volte à conversa da imagem antes de anexar.');
        // Applying explicitly replaces a previous local region, including a different source.
        const result = await api('/selection', {
          method: 'POST', body: JSON.stringify({ chat_id: state.chatId, number: state.number })
        });
        setSelection(state.chatId, result.selected_number);
        clearDraft();
        draft = { chatId: state.chatId, path: location.pathname, number: state.number, maskPNG: pngURI, resource: retainSource(resource), identity: Symbol('mask') };
        renderChip();
        announce(`Área da imagem ${state.number} anexada. Descreva a mudança e ative Imagem antes de enviar.`);
        working = false;
        dialog.close();
        document.getElementById('chat-input')?.focus();
      } catch (error) { status.textContent = error.message; }
      finally { output.width = output.height = 0; working = false; controls(); }
    }

    const resize = new ResizeObserver(() => {
      // Fit only until the user changes zoom; later preserve their working position.
      if (!state.fitted) { fit(); state.fitted = true; }
    });
    resize.observe(stage);
    dialog.addEventListener('close', () => {
      state.closed = true;
      resize.disconnect();
      image.removeAttribute('src');
      mask.width = mask.height = base.width = base.height = 0;
      history.length = 0;
      current = null;
      releaseSource(resource);
      dialog.remove();
      if (editor === state) editor = null;
      if (state.opener?.isConnected) state.opener.focus({ preventScroll: true });
    }, { once: true });
    dialog.showModal();
    controls();
    brush.focus();
    if (draft?.chatId === state.chatId && draft.number === state.number) {
      working = true; controls();
      const stored = new Image();
      stored.onload = () => {
        if (state.closed) return;
        baseContext.drawImage(stored, 0, 0);
        for (let y = 0; y < mask.height; y += 128) {
          const pixels = baseContext.getImageData(0, y, mask.width, Math.min(128, mask.height - y));
          for (let i = 0; i < pixels.data.length; i += 4) {
            const alpha = pixels.data[i] >= 128 ? 255 : 0;
            pixels.data[i] = 255; pixels.data[i + 1] = 82; pixels.data[i + 2] = 126; pixels.data[i + 3] = alpha;
          }
          baseContext.putImageData(pixels, 0, y);
        }
        redraw(); working = false; controls();
      };
      stored.onerror = () => { if (!state.closed) { status.textContent = 'Não foi possível recuperar a área. Cancele e tente novamente.'; working = false; controls(); } };
      stored.src = draft.maskPNG;
    }
  }

  function lastUserText(payload) {
    let userMessage = payload.user_message;
    if (!userMessage || userMessage.role !== 'user') {
      userMessage = Array.isArray(payload.messages) ? [...payload.messages].reverse().find(message => message.role === 'user') : null;
    }
    if (typeof userMessage?.content === 'string') return userMessage.content;
    if (Array.isArray(userMessage?.content)) {
      const parts = userMessage.content.filter(part => part.type === 'text');
      if (parts.every(part => typeof part.text === 'string')) return parts.map(part => part.text).join('\n');
    }
    return null;
  }

  function composerElement() {
    const input = document.getElementById('chat-input');
    return input?.isContentEditable ? input : input?.querySelector('[contenteditable="true"]') || input;
  }

  function composerText() {
    return (composerElement()?.innerText || '').replace(/\u200b/g, '').trim();
  }

  // Upstream clears the editor before fetch. Restore the exact text through the
  // editor's own paste handling, never by writing editor DOM directly.
  function restoreComposer(text, attempt = 0) {
    if (typeof text !== 'string' || !text.trim() || composerText()) return;
    const element = composerElement();
    if (element) {
      element.focus({ preventScroll: true });
      try {
        const data = new DataTransfer();
        data.setData('text/plain', text);
        element.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
      } catch { /* The command fallback below handles engines without synthetic paste. */ }
      if (!composerText()) document.execCommand('insertText', false, text);
      if (composerText()) return;
    }
    if (attempt < 12) setTimeout(() => restoreComposer(text, attempt + 1), 150);
  }

  function rejected(message, text) {
    announce(message, true);
    if (text) setTimeout(() => restoreComposer(text), 0);
    return new Response(JSON.stringify({ detail: message }), { status: 409, headers: { 'Content-Type': 'application/json' } });
  }

  function imageToggleState(chatId) {
    // Fail open: only an unambiguous Off state may stop a keystroke before upstream clears the composer.
    if (document.getElementById('message-input-container')?.querySelector('button[class~="text-sky-500"]')) return true;
    try {
      const stored = JSON.parse(sessionStorage.getItem(`chat-input-${chatId}`) || 'null');
      if (typeof stored?.imageGenerationEnabled === 'boolean') return stored.imageGenerationEnabled;
    } catch { /* Drafts may be unavailable in temporary chats. */ }
    return null;
  }

  function draftBlock(imageOn) {
    const pending = draft;
    if (!pending) return '';
    if (currentChat() !== pending.chatId) return 'A área anexada pertence a outra conversa. Volte à conversa da imagem ou remova a área antes de enviar.';
    if (sending) return 'Aguarde o envio da área terminar antes de enviar outra mensagem.';
    if (editor) return 'Aplique ou cancele a área no editor antes de enviar.';
    if (imageOn === false) return 'Ative Imagem no compositor para enviar uma edição com área, ou remova a área para enviar só texto. Sua mensagem foi mantida.';
    if (selections.get(pending.chatId) !== pending.number) {
      return `A área pertence à imagem ${pending.number}, mas outra base está selecionada. Ajuste a área ou remova-a antes de enviar.`;
    }
    return '';
  }

  function guardSend(event) {
    if (!draft || event.defaultPrevented) return;
    let sendIntent = false;
    if (event.type === 'keydown') {
      if (!(event.key === 'Enter' || event.keyCode === 13) || event.isComposing || event.shiftKey ||
          !event.target?.closest?.('#chat-input') || document.getElementById('suggestions-container')) return;
      let ctrlOnly = false;
      try { ctrlOnly = JSON.parse(localStorage.getItem('settings') || '{}').ctrlEnterToSend === true; } catch { /* Default Enter send. */ }
      const touch = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
      sendIntent = ctrlOnly ? (event.ctrlKey || event.metaKey) : !touch;
    } else sendIntent = !!event.target?.closest?.('#send-message-button');
    if (!sendIntent || !composerText()) return;
    const reason = draftBlock(imageToggleState(draft.chatId));
    if (!reason) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    announce(reason, true);
  }

  document.addEventListener('keydown', guardSend, true);
  document.addEventListener('click', guardSend, true);

  // Esc in the message field: Open WebUI would stop the response AND clear every toggle
  // (tools, filters, web search, image generation). Keep only the stop.
  // Open WebUI 0.12.0 renders Stop only while a reply streams with an empty field; otherwise
  // the same place holds Send, Voice Input or Voice mode (a call), which Esc must never press.
  // Stop is recognized by its accessible label: the 0.12.0 translations of "Stop" (en, pt-BR,
  // pt-PT, es-ES, fr-FR, de-DE, it-IT). Any other language: Esc still keeps the toggles.
  const STOP_LABEL = /^(stop|parar|detener|stopp|arresta)$/i;
  function stopButton(field) {
    const form = field.closest('form');
    if (!form) return null;
    return [...form.querySelectorAll('button[aria-label]:not(:disabled)')]
      .find(button => STOP_LABEL.test(button.getAttribute('aria-label').trim()) &&
        button.getClientRects().length) || null;
  }

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.isComposing) return;
    const field = event.target?.closest?.('#chat-input');
    if (!field || document.getElementById('suggestions-container')) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    stopButton(field)?.click();
  }, true);

  /* `[` in the message field: an IDE-style hint offers the two note forms the image filter
   * removes before the router and the model (strip_ignored, DEC-02), and every complete note
   * is drawn fainter in the field. Nothing here rewrites the field: the hint inserts only
   * what the user picks, and the fading is a CSS highlight over the existing text. */
  const IGNORED = /\[\s*['\u2018\u2019][^[\]]*['\u2018\u2019]\s*\]|\[\s*ign[oòóôõö]rar\s*:[^[\]]*\]/gi;
  const NOTE_FORMS = [
    { label: '[\'…\']', text: '[\'\']', caret: 2, help: 'comentário' },
    { label: '[Ignorar: …]', text: '[Ignorar: ]', caret: 10, help: 'nota para o desenvolvedor' }
  ];
  const highlights = window.CSS?.highlights && typeof Highlight === 'function' ? CSS.highlights : null;
  let bracketHint = null;
  let ignoredQueued = false;

  // Open WebUI 0.12.0's field is a Tiptap editor; Tiptap exposes itself on its root element.
  function richEditor(field) {
    return field?.editor?.view && field.editor.state ? field.editor : null;
  }

  function caretAfterBracket(field) {
    const rich = richEditor(field);
    if (rich) {
      const { from, empty } = rich.state.selection;
      return empty && from > 0 && rich.state.doc.textBetween(from - 1, from) === '[' ? { pos: from } : null;
    }
    const selection = getSelection();
    const { anchorNode, anchorOffset } = selection || {};
    return selection?.isCollapsed && field.contains(anchorNode) && anchorNode.nodeType === Node.TEXT_NODE &&
      anchorOffset > 0 && anchorNode.data[anchorOffset - 1] === '[' ? { node: anchorNode, offset: anchorOffset } : null;
  }

  function caretStill(hint) {
    const at = hint.field.isConnected && caretAfterBracket(hint.field);
    return !!at && (at.pos !== undefined ? at.pos === hint.at.pos : at.node === hint.at.node && at.offset === hint.at.offset);
  }

  function closeBracketHint() {
    bracketHint?.element.remove();
    bracketHint = null;
  }

  function markBracketHint() {
    bracketHint.options.forEach((option, index) => {
      option.setAttribute('aria-selected', String(index === bracketHint.active));
    });
  }

  function openBracketHint(field, at) {
    closeBracketHint();
    const element = node('div', 'pepito-bracket-hint');
    element.setAttribute('role', 'listbox');
    element.setAttribute('aria-label', 'Inserir trecho ignorado');
    const options = NOTE_FORMS.map((form, index) => {
      const option = node('div', 'pepito-bracket-option');
      option.setAttribute('role', 'option');
      option.append(node('code', '', form.label), node('span', '', form.help));
      // The field keeps the focus (and its caret) while an option is clicked.
      option.addEventListener('mousedown', event => event.preventDefault());
      option.addEventListener('click', () => {
        if (!bracketHint) return;
        bracketHint.active = index;
        acceptBracketHint();
      });
      return option;
    });
    element.append(...options, node('p', 'pepito-bracket-keys', 'Tab ou Enter completa · Esc fecha · não vai ao modelo'));
    document.body.append(element);
    bracketHint = { element, options, active: 0, field, at };
    markBracketHint();
    const selection = getSelection();
    let rect = selection?.rangeCount ? selection.getRangeAt(0).getBoundingClientRect() : null;
    if (!rect || (!rect.top && !rect.left && !rect.height)) rect = field.getBoundingClientRect();
    const above = rect.top - element.offsetHeight - 6;
    element.style.left = `${Math.max(8, Math.min(innerWidth - element.offsetWidth - 8, rect.left - 8))}px`;
    element.style.top = `${above >= 8 ? above : rect.bottom + 6}px`;
  }

  function acceptBracketHint() {
    const hint = bracketHint;
    closeBracketHint();
    if (!hint || !caretStill(hint)) return;
    const form = NOTE_FORMS[hint.active];
    const rich = richEditor(hint.field);
    if (rich) {
      // A plain text transaction: no input rule (smart quotes) rewrites what is inserted.
      const from = hint.at.pos - 1;
      rich.view.dispatch(rich.state.tr.insertText(form.text, from, hint.at.pos));
      rich.chain().setTextSelection(from + form.caret).focus().run();
    } else {
      const selection = getSelection();
      selection.modify('extend', 'backward', 'character');
      document.execCommand('insertText', false, form.text);
      for (let moved = form.caret; moved < form.text.length; moved += 1) selection.modify('move', 'backward', 'character');
    }
    refreshIgnored();
  }

  // Runs before the document-level listeners (guardSend, the SPEC-107 Esc guard) and before
  // the editor: with the hint open, these keys belong to the hint alone.
  window.addEventListener('keydown', event => {
    const hint = bracketHint;
    if (!hint || event.isComposing) return;
    if (!event.target?.closest?.('#chat-input')) { closeBracketHint(); return; }
    const plain = !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;
    if (plain && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      hint.active = (hint.active + (event.key === 'ArrowDown' ? 1 : hint.options.length - 1)) % hint.options.length;
      markBracketHint();
    } else if (plain && (event.key === 'Tab' || event.key === 'Enter')) {
      acceptBracketHint();
    } else if (event.key === 'Escape') {
      // Only the hint closes: no toggle is cleared and a streaming reply keeps going.
      closeBracketHint();
    } else {
      if (!['Shift', 'Control', 'Alt', 'AltGraph', 'Meta', 'CapsLock'].includes(event.key)) closeBracketHint();
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  document.addEventListener('input', event => {
    const field = event.target?.closest?.('#chat-input');
    if (!field) return;
    queueIgnored();
    if (event.inputType !== 'insertText' || !event.data?.endsWith('[')) return;
    // The editor reads the typed text into its state after this event.
    requestAnimationFrame(() => {
      const at = viewer || editor || document.activeElement !== field ? null : caretAfterBracket(field);
      if (at) openBracketHint(field, at);
    });
  }, true);
  document.addEventListener('selectionchange', () => { if (bracketHint && !caretStill(bracketHint)) closeBracketHint(); });
  document.addEventListener('focusout', event => { if (event.target === bracketHint?.field) closeBracketHint(); }, true);
  window.addEventListener('resize', closeBracketHint);

  function queueIgnored() {
    if (ignoredQueued || !highlights) return;
    ignoredQueued = true;
    requestAnimationFrame(refreshIgnored);
  }

  function refreshIgnored() {
    ignoredQueued = false;
    if (!highlights) return;
    const field = composerElement();
    const ranges = [];
    if (field) {
      // The field's text, a line break between blocks, mapped back to its text nodes.
      let text = '';
      let block = null;
      const pieces = [];
      const walker = document.createTreeWalker(field, NodeFilter.SHOW_TEXT);
      for (let item = walker.nextNode(); item; item = walker.nextNode()) {
        if (!item.data) continue;
        const parent = item.parentElement?.closest('p,li,h1,h2,h3,h4,h5,h6,pre,blockquote,td,th') || field;
        if (block && parent !== block) text += '\n';
        block = parent;
        pieces.push({ node: item, start: text.length });
        text += item.data;
      }
      const locate = index => {
        let piece = pieces[0];
        for (const item of pieces) { if (item.start > index) break; piece = item; }
        return [piece.node, index - piece.start];
      };
      for (const match of text.matchAll(IGNORED)) {
        const range = document.createRange();
        range.setStart(...locate(match.index));
        const [last, offset] = locate(match.index + match[0].length - 1);
        range.setEnd(last, offset + 1);
        ranges.push(range);
      }
    }
    if (ranges.length) highlights.set('pepito-ignored', new Highlight(...ranges));
    else highlights.delete('pepito-ignored');
  }

  async function cancelArm(chatId, draftId) {
    if (!draftId) return;
    try { await api(`/draft?chat_id=${encodeURIComponent(chatId)}&draft_id=${encodeURIComponent(draftId)}`, { method: 'DELETE' }); }
    catch { announce('O envio não foi aceito e a área local foi preservada. A autorização temporária expirará em até 5 minutos; tente enviar novamente.', true); }
  }

  // Exactly one wrapper. Inspect a cloned Request when necessary, never consume/mutate it.
  window.fetch = async function pepitoFetch(input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
    const method = (init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (!draft || url.origin !== location.origin || url.pathname !== '/api/chat/completions' || method !== 'POST') {
      return originalFetch.call(this, input, init);
    }
    let payload;
    try {
      const body = init?.body ?? (input instanceof Request ? await input.clone().text() : null);
      if (typeof body !== 'string') return rejected('Não foi possível vincular a área ao pedido. Remova a área ou tente novamente nesta conversa.');
      payload = JSON.parse(body);
    } catch { return rejected('Não foi possível ler o pedido. A área foi preservada; remova-a ou tente novamente.'); }
    const pending = draft;
    const text = lastUserText(payload);
    const reason = payload.chat_id !== pending.chatId
      ? 'A área anexada pertence a outra conversa. Volte à conversa da imagem ou remova a área antes de enviar.'
      : draftBlock(payload.features?.image_generation === true);
    if (reason) return rejected(reason, text);
    if (text === null || !text.trim()) return rejected('Escreva a mudança desejada antes de enviar a área.');
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    if (signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
    sending = true;
    renderChip();
    for (const mount of mounts) mount.updateLabels?.();
    let armedId = null;
    try {
      const armed = await api('/draft', {
        method: 'POST', signal,
        body: JSON.stringify({ chat_id: pending.chatId, source_number: pending.number, mask_png: pending.maskPNG, text })
      });
      armedId = armed.draft_id;
      if (!armedId || armed.source_number !== pending.number) throw new Error('O servidor não confirmou a área. Tente novamente.');
      if (signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
      const response = await originalFetch.call(this, input, init);
      if (response.ok) {
        if (draft?.identity === pending.identity) clearDraft();
        announce('Pedido enviado com a área selecionada.');
      } else {
        await cancelArm(pending.chatId, armedId);
        setTimeout(() => restoreComposer(text), 0);
        announce('O envio foi recusado. Sua área e sua mensagem foram mantidas; corrija o erro e envie novamente.', true);
      }
      return response;
    } catch (error) {
      await cancelArm(pending.chatId, armedId);
      if (error?.name === 'AbortError') {
        announce('Envio cancelado. Sua área continua anexada.', true);
        throw error;
      }
      return rejected(`A área não foi enviada. ${error.message} Sua seleção foi preservada; tente novamente ou remova a área.`, text);
    } finally {
      sending = false;
      renderChip();
      for (const mount of mounts) mount.updateLabels?.();
    }
  };

  function scan() {
    scanQueued = false;
    const chatId = currentChat();
    if (observedChat !== chatId) {
      observedChat = chatId;
      closeBracketHint();
      purgeSources(resource => resource.chatId === chatId);
      if (editor?.dialog?.open) editor.dialog.close();
      // A local region is deliberately pinned, never migrated to a new chat/source.
      if (draft && ((chatId && draft.chatId !== chatId) || draft.path !== location.pathname)) {
        if (!sending) {
          clearDraft();
          announce('A área local foi removida ao trocar de conversa. A instrução de texto não foi alterada.');
        }
      }
    }
    for (const mount of mounts) {
      if (!mount.frame.isConnected || mount.path !== location.pathname ||
          (chatId && mount.chatId && mount.chatId !== chatId) || markerOf(mount.frame) !== mount.group) destroyMount(mount);
      else hideStock(mount);
    }
    for (const frame of document.querySelectorAll('[id^="message-"] iframe[srcdoc]')) {
      if (frames.has(frame)) continue;
      const group = markerOf(frame);
      const message = frame.closest('[id^="message-"]');
      if (!group || !message) continue;
      const mount = { frame, group, chatId, path: location.pathname, messageId: message.id.slice('message-'.length), state: 'waiting', hidden: new Set(), ready: false };
      frames.set(frame, mount);
      mounts.add(mount);
      visibility.observe(frame);
    }
    if (draft && (!chip?.isConnected || !document.getElementById('message-input-container')?.contains(chip))) renderChip();
    renderPreferenceChip();
    // The editor replaces its text nodes on send, restore and chat change.
    queueIgnored();
  }

  function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    requestAnimationFrame(scan);
  }

  function start() {
    observedChat = currentChat();
    visibility = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const mount = frames.get(entry.target);
        if (!mount) continue;
        if (entry.isIntersecting && mount.state === 'waiting') upgrade(mount);
        else if (entry.target === mount.section && mount.state === 'mounted') {
          if (entry.isIntersecting && !mount.resource) mount.show(mount.index).catch(report);
          else if (!entry.isIntersecting) mount.pause();
        }
      }
    }, { rootMargin: '180px' });
    observer = new MutationObserver(queueScan);
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['srcdoc', 'src', 'id'] });
    window.addEventListener('popstate', queueScan);
    window.addEventListener('pagehide', event => {
      if (event.persisted) return;
      if (viewer?.dialog.open) viewer.dialog.close();
      observer.disconnect(); visibility.disconnect();
      if (editor?.dialog?.open) editor.dialog.close();
      clearDraft();
      for (const mount of [...mounts]) destroyMount(mount);
      selections.clear();
      purgeSources();
    });
    // SvelteKit uses history.pushState/replaceState; its ensuing DOM mutations
    // trigger scan. No history monkeypatch or polling is required.
    scan();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
