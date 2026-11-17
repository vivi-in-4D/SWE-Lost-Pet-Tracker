document.addEventListener("DOMContentLoaded", startPage);

function startPage() {
  connectForms();
  connectLocationButton();
  connectBrowsePage();
  showSignedInContent();
}


// ----- talking to the server -----

// sends a form, or just fetches the address when no form is given.
async function sendToServer(address, form) {
  const options = form ? { method: "POST", body: new FormData(form) } : {};
  const response = await fetch(address, options);
  const reply = await response.json().catch(() => ({}));

  if (!response.ok) {
    const error = new Error(reply.error || "Something went wrong.");
    error.field = reply.field;
    throw error;
  }

  return reply;
}


// ----- forms -----
// a form (or one of its buttons) names its server address in data-api,
// and what should happen afterwards in data-then.

const NEXT_STEPS = {
  showCodeStep(reply) {
    document.getElementById("report-section").hidden = true;
    document.getElementById("code-section").hidden = false;
    document.querySelector("#code-section [name=account_number]").value = reply.account_number;
  },

  goToMatches() {
    location.href = "matches.html";
  },

  reloadPage() {
    location.reload();
  },

  showResults(reply) {
    const count = reply.reports.length;
    const status = document.getElementById("results-status");

    if (count === 0) {
      status.textContent = "No reports found. Try a wider search distance or fewer filters.";
    } else if (count === 1) {
      status.textContent = "1 report found.";
    } else {
      status.textContent = `${count} reports found.`;
    }

    document.getElementById("report-list").innerHTML = reply.reports.map(browsedReportHtml).join("");
  },
};

function connectForms() {
  for (const form of document.forms) {
    form.addEventListener("submit", submitForm);
  }
}

async function submitForm(event) {
  event.preventDefault();

  const form = event.target;
  const button = event.submitter;
  const settings = button && button.dataset.api ? button.dataset : form.dataset;
  const message = form.querySelector(".message");

  try {
    const reply = await sendToServer(settings.api, form);
    message.className = "message";
    message.textContent = reply.message || "";

    const nextStep = NEXT_STEPS[settings.then];
    if (nextStep) {
      nextStep(reply);
    }
  } catch (error) {
    message.className = "message error";
    message.textContent = error.message;

    const field = form.elements[error.field];
    if (field && field.focus) {
      field.focus();
    }
  }
}

function connectLocationButton() {
  const button = document.getElementById("use-location");
  if (!button) {
    return;
  }

  button.addEventListener("click", () => {
    navigator.geolocation.getCurrentPosition(fillLocation, locationRefused);
  });
}

function fillLocation(position) {
  const latitude = position.coords.latitude.toFixed(5);
  const longitude = position.coords.longitude.toFixed(5);
  document.querySelector("[name=location]").value = `${latitude}, ${longitude}`;
}

function locationRefused() {
  alert("Location access was not allowed. Type a city or coordinates instead.");
}


// ----- browse reports page -----

function connectBrowsePage() {
  const form = document.getElementById("filter-form");
  if (!form) {
    return;
  }

  const breedField = document.getElementById("breed-field");
  const species = form.elements.species;

  species.addEventListener("change", () => {
    breedField.hidden = species.value === "other";
    if (breedField.hidden) {
      form.elements.breed.value = "";
    }
  });

  form.addEventListener("reset", () => {
    breedField.hidden = false;
    // the fields are cleared just after this event, so search again on the next tick
    setTimeout(() => form.requestSubmit());
  });

  form.requestSubmit();
}


// ----- my report and matches pages -----

async function showSignedInContent() {
  const reportBox = document.getElementById("my-report");
  const matchList = document.getElementById("match-list");
  if (!reportBox && !matchList) {
    return;
  }

  const page = await sendToServer("/api/my");
  const signedIn = Boolean(page.report);
  document.body.classList.add(signedIn ? "signed-in" : "signed-out");
  if (!signedIn) {
    return;
  }

  if (reportBox) {
    reportBox.innerHTML =
      reportHtml(page.report) +
      "<h2>My contact details</h2>" +
      contactsHtml(page.contacts);
  }

  if (matchList) {
    matchList.innerHTML = matchesHtml(page.matches);
  }
}


// ----- building HTML -----

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (character) => `&#${character.charCodeAt(0)};`);
}

// the photos and details of one report. used for your own report and for each match.
function reportHtml(report) {
  const isLost = report.posttype === "lost";

  const rows = [
    ["Type", isLost ? "Lost pet" : "Found pet"],
    ["Species", report.species],
    ["Breed", report.breed],
    ["Colors", report.colors.join(", ")],
    ["Markings", report.markings.join("; ")],
    ["Weight", report.weight ? `${report.weight} kg` : ""],
    ["Sex", report.sex],
    [isLost ? "Last seen" : "Found", new Date(report.lostorfoundat).toLocaleString()],
    ["Location", `${report.latitude.toFixed(4)}, ${report.longitude.toFixed(4)}`],
    ["Details", report.description],
  ];

  const photos = report.photos
    .map((address) => `<img src="${escapeHtml(address)}" alt="Photo for the report ${escapeHtml(report.title)}">`)
    .join("");

  const details = rows
    .filter(([label, value]) => value)
    .map(([label, value]) => `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");

  return `<h2>${escapeHtml(report.title)}</h2>${photos}<dl class="details">${details}</dl>`;
}

function contactsHtml(contacts) {
  const items = contacts
    .map((contact) => `<li>${escapeHtml(contact.type)}: ${escapeHtml(contact.value)}</li>`)
    .join("");

  return `<ul>${items}</ul>`;
}

function matchesHtml(matches) {
  if (matches.length === 0) {
    return "<li>No strong matches right now. We will email you if a new report looks like a match.</li>";
  }

  return matches.map(matchHtml).join("");
}

function matchHtml(match) {
  const percent = Math.round(match.score * 100);
  const contact = contactSectionHtml(
    match.contacts,
    "This poster's contact details are in your match notification email."
  );

  return `
    <li class="match-card">
      <p class="score">Match score: ${percent}%</p>
      <p>${escapeHtml(match.explanation)}</p>
      ${reportHtml(match.report)}
      ${contact}
    </li>`;
}

function browsedReportHtml(item) {
  const contact = contactSectionHtml(
    item.contacts,
    "This poster has not made their contact details public."
  );

  return `
    <li class="match-card">
      ${reportHtml(item.report)}
      ${contact}
    </li>`;
}

function contactSectionHtml(contacts, textWhenNone) {
  if (contacts.length === 0) {
    return `<p>${textWhenNone}</p>`;
  }

  return `<h3>Contact</h3>${contactsHtml(contacts)}`;
}
