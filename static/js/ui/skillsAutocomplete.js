import { API } from '../constants.js';

let skills = [];
let dropdownEl = null;
let activeIndex = -1;

/**
 * Fetch available skills from the server and cache them.
 */
export async function loadSkills() {
    try {
        const res = await fetch(`${API}/skills`);
        if (res.ok) {
            skills = await res.json();
        }
    } catch (e) {
        console.warn('Failed to load skills:', e);
    }
}

/**
 * Initialize the slash-command autocomplete on the user input.
 */
export function initSkillsAutocomplete() {
    const input = document.getElementById('user-input');
    if (!input) return;

    // Create the dropdown element
    dropdownEl = document.createElement('div');
    dropdownEl.className = 'skills-dropdown';
    dropdownEl.style.display = 'none';
    input.parentElement.appendChild(dropdownEl);

    input.addEventListener('input', () => handleInput(input));
    input.addEventListener('keydown', (e) => handleKeydown(e, input));

    // Close on outside click
    document.addEventListener('click', (e) => {
        if (!dropdownEl.contains(e.target) && e.target !== input) {
            hideDropdown();
        }
    });
}

function handleInput(input) {
    const value = input.value;

    // Only trigger on / at the start of input (possibly after whitespace)
    if (!value.trimStart().startsWith('/')) {
        hideDropdown();
        return;
    }

    // Extract the command being typed (everything up to first space after /)
    const trimmed = value.trimStart();
    const spaceIndex = trimmed.indexOf(' ');
    const typedCommand = spaceIndex === -1 ? trimmed : trimmed.substring(0, spaceIndex);

    // If user already typed a full command + space, hide dropdown (they're typing args)
    if (spaceIndex !== -1) {
        const matchedSkill = skills.find(s => s.command.toLowerCase() === typedCommand.toLowerCase());
        if (matchedSkill) {
            hideDropdown();
            showInputHint(matchedSkill, input);
            return;
        }
    }

    // Filter skills matching what's been typed
    const matches = skills.filter(s =>
        s.command.toLowerCase().startsWith(typedCommand.toLowerCase())
    );

    if (matches.length === 0) {
        hideDropdown();
        return;
    }

    showDropdown(matches, input);
}

function handleKeydown(e, input) {
    if (dropdownEl.style.display === 'none') return;

    const items = dropdownEl.querySelectorAll('.skills-dropdown-item');
    if (!items.length) return;

    if (e.key === 'ArrowDown') {
        e.preventDefault();
        activeIndex = (activeIndex + 1) % items.length;
        updateActiveItem(items);
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        activeIndex = (activeIndex - 1 + items.length) % items.length;
        updateActiveItem(items);
    } else if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        const selected = items[activeIndex >= 0 ? activeIndex : 0];
        if (selected) {
            selectSkill(selected.dataset.command, input);
        }
    } else if (e.key === 'Escape') {
        hideDropdown();
    }
}

/**
 * Returns true if the skills dropdown is currently visible.
 */
export function isDropdownVisible() {
    return dropdownEl && dropdownEl.style.display !== 'none';
}

function showDropdown(matches, input) {
    dropdownEl.innerHTML = '';
    activeIndex = -1;

    matches.forEach((skill, i) => {
        const item = document.createElement('div');
        item.className = 'skills-dropdown-item';
        item.dataset.command = skill.command;

        const cmd = document.createElement('span');
        cmd.className = 'skills-cmd';
        cmd.textContent = skill.command;

        const desc = document.createElement('span');
        desc.className = 'skills-desc';
        desc.textContent = skill.description;

        item.appendChild(cmd);
        item.appendChild(desc);

        item.addEventListener('click', () => selectSkill(skill.command, input));
        item.addEventListener('mouseenter', () => {
            activeIndex = i;
            updateActiveItem(dropdownEl.querySelectorAll('.skills-dropdown-item'));
        });

        dropdownEl.appendChild(item);
    });

    dropdownEl.style.display = 'block';
}

function hideDropdown() {
    if (dropdownEl) {
        dropdownEl.style.display = 'none';
        activeIndex = -1;
    }
    // Remove any input hint
    const hint = document.querySelector('.skills-input-hint');
    if (hint) hint.remove();
}

function selectSkill(command, input) {
    // Replace input value with the selected command + space (ready for args)
    input.value = command + ' ';
    input.focus();
    hideDropdown();

    // Show hint for inputs
    const skill = skills.find(s => s.command === command);
    if (skill) {
        showInputHint(skill, input);
    }
}

function showInputHint(skill, input) {
    // Remove existing hint
    let hint = document.querySelector('.skills-input-hint');
    if (hint) hint.remove();

    const requiredInputs = (skill.inputs || []).filter(i => i.required);
    if (requiredInputs.length === 0) return;

    hint = document.createElement('div');
    hint.className = 'skills-input-hint';
    hint.textContent = requiredInputs.map(i => i.placeholder || i.name).join(', ');
    input.parentElement.appendChild(hint);

    // Remove hint when input loses focus or value changes away from this command
    const cleanup = () => {
        if (hint.parentElement) hint.remove();
        input.removeEventListener('blur', cleanup);
    };
    input.addEventListener('blur', cleanup, { once: true });
}

function updateActiveItem(items) {
    items.forEach((item, i) => {
        item.classList.toggle('active', i === activeIndex);
    });
}

export function getSkills() {
    return skills;
}
