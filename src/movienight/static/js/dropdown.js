// Compact dropdown chip (icon + label + chevron) with a menu of pressable items. Shared by the Tonight
// duration control and the era control so both look and behave the same (keyboard, Escape, outside click).
import { h, icon } from './dom.js';

let uid = 0;

// `build()` returns the <ul> for the menu; it runs each time the menu opens.
export function dropdown({ iconName, build }) {
  const menuId = `dd-menu-${++uid}`;
  const label = h('span', { class: 'chip__label' });
  const button = h('button', {
    type: 'button', class: 'chip chip--time', 'aria-haspopup': 'true', 'aria-expanded': 'false', 'aria-controls': menuId,
    onclick: () => toggle(menu.hidden),
  }, icon(iconName), label, icon('chevron', 'icon--end'));
  const menu = h('div', { class: 'menu menu--down menu--time', id: menuId, hidden: true });
  const chip = h('div', { class: 'menu-wrap timectl' }, button, menu);

  function onOutside(e) { if (!chip.contains(e.target)) toggle(false); }
  function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); toggle(false); button.focus(); } }
  function toggle(open) {
    if (open) menu.replaceChildren(build());
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) {
      document.addEventListener('pointerdown', onOutside);
      chip.addEventListener('keydown', onKey);
      (menu.querySelector('[aria-pressed="true"]') ?? menu.querySelector('button'))?.focus();
    } else {
      document.removeEventListener('pointerdown', onOutside);
      chip.removeEventListener('keydown', onKey);
    }
  }

  // One selectable row; the menu closes and focus returns to the chip before `onPick` runs.
  function item(text, hint, on, onPick) {
    return h('li', null, h('button', {
      type: 'button', class: 'menu__item', 'aria-pressed': String(on),
      onclick: () => { toggle(false); button.focus(); onPick(); },
    }, h('span', { text }), hint ? h('span', { class: 'menu__hint', text: hint }) : null));
  }

  return { chip, button, label, toggle, item };
}
