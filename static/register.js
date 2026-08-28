document.addEventListener("DOMContentLoaded", function () {
  var container = document.getElementById("member-rows");
  var template = document.getElementById("member-row-template");
  var addBtn = document.getElementById("add-member-btn");

  function addRow() {
    var clone = template.content.cloneNode(true);
    var row = clone.querySelector(".member-row");
    row.querySelector(".remove-row").addEventListener("click", function () {
      row.remove();
    });
    // The checkbox itself is never submitted when unchecked, which would
    // shift every later row's member_*[] fields out of alignment -- the
    // hidden sibling always submits a "0"/"1" so the lists stay in sync.
    var checkbox = row.querySelector(".member-id-verified-checkbox");
    var hidden = checkbox.nextElementSibling;
    checkbox.addEventListener("change", function () {
      hidden.value = checkbox.checked ? "1" : "0";
    });
    container.appendChild(clone);
  }

  addBtn.addEventListener("click", addRow);

  // Start with one empty member row so the form doesn't look empty.
  addRow();
});
