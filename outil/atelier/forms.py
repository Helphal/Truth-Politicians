from django import forms

from .models import (
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    EvidencePassage,
    HumanReview,
    Measure,
    MeasureScrutinyLink,
    Scrutiny,
)


class ReviewActionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_digest = forms.CharField(min_length=64, max_length=64, widget=forms.HiddenInput)
    decision = forms.ChoiceField(
        choices=[
            (HumanReview.Decision.APPROVE, "Approuver cette révision (sans mise en ligne)"),
            (HumanReview.Decision.REQUEST_EVIDENCE, "Demander un complément"),
            (HumanReview.Decision.REJECT, "Écarter"),
        ]
    )
    note = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))


class CorrectionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_digest = forms.CharField(min_length=64, max_length=64, widget=forms.HiddenInput)
    title = forms.CharField(label="Titre", max_length=350)
    short_answer = forms.CharField(label="Réponse courte", widget=forms.Textarea(attrs={"rows": 3}))
    main_caveat = forms.CharField(label="Réserve principale", widget=forms.Textarea(attrs={"rows": 3}))
    reasoning = forms.CharField(label="Raisonnement", widget=forms.Textarea(attrs={"rows": 6}))
    change_summary = forms.CharField(label="Motif de la correction", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))


class DeclarationMeasureQualificationForm(forms.Form):
    declaration = forms.ModelChoiceField(queryset=Declaration.objects.none(), label="Déclaration publique")
    measure = forms.ModelChoiceField(queryset=Measure.objects.none(), label="Mesure concrète")
    stance = forms.ChoiceField(choices=DeclarationMeasureLink.Stance.choices, label="Sens de la position")
    qualification_status = forms.ChoiceField(choices=DocumentaryQualification.choices, label="État documentaire")
    evidence = forms.ModelChoiceField(queryset=EvidencePassage.objects.none(), label="Passage qui justifie ce lien")
    rationale = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}),
        min_length=10,
        max_length=4000,
        label="Pourquoi cette parole vise-t-elle cette mesure ?",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["declaration"].queryset = Declaration.objects.order_by("speaker_label", "stable_id")
        self.fields["measure"].queryset = Measure.objects.order_by("title", "stable_id")
        self.fields["evidence"].queryset = EvidencePassage.objects.select_related("document__source").order_by(
            "document__source__producer", "pk"
        )

    def clean(self):
        cleaned = super().clean()
        declaration = cleaned.get("declaration")
        evidence = cleaned.get("evidence")
        if declaration and evidence and evidence.pk != declaration.source_passage_id:
            self.add_error("evidence", "Choisissez le passage source exact de cette déclaration.")
        if (
            cleaned.get("qualification_status") == DocumentaryQualification.QUALIFIED
            and cleaned.get("stance") == DeclarationMeasureLink.Stance.UNCLEAR
        ):
            self.add_error("stance", "Une relation qualifiée doit avoir un sens déterminé.")
        return cleaned


class MeasureScrutinyQualificationForm(forms.Form):
    measure = forms.ModelChoiceField(queryset=Measure.objects.none(), label="Mesure concrète")
    scrutiny = forms.ModelChoiceField(queryset=Scrutiny.objects.none(), label="Scrutin officiel")
    vote_effect = forms.ChoiceField(choices=MeasureScrutinyLink.VoteEffect.choices, label="Effet d’un vote pour")
    qualification_status = forms.ChoiceField(choices=DocumentaryQualification.choices, label="État documentaire")
    evidence = forms.ModelChoiceField(queryset=EvidencePassage.objects.none(), label="Passage qui établit l’objet du vote")
    rationale = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}),
        min_length=10,
        max_length=4000,
        label="Pourquoi ce scrutin fait-il avancer ou bloque-t-il cette mesure ?",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["measure"].queryset = Measure.objects.order_by("title", "stable_id")
        self.fields["scrutiny"].queryset = Scrutiny.objects.filter(identity_correction__isnull=True).order_by(
            "-occurred_on", "stable_id"
        )
        self.fields["evidence"].queryset = EvidencePassage.objects.select_related("document__source").order_by(
            "document__source__producer", "pk"
        )

    def clean(self):
        cleaned = super().clean()
        scrutiny = cleaned.get("scrutiny")
        evidence = cleaned.get("evidence")
        if scrutiny and evidence and evidence.document_id not in {
            scrutiny.source_document_id,
            scrutiny.submitted_text.source_document_id,
        }:
            self.add_error("evidence", "Choisissez un passage du scrutin ou de la version de texte soumise.")
        if (
            cleaned.get("qualification_status") == DocumentaryQualification.QUALIFIED
            and cleaned.get("vote_effect") == MeasureScrutinyLink.VoteEffect.UNCLEAR
        ):
            self.add_error("vote_effect", "Une relation qualifiée doit préciser l’effet du vote.")
        return cleaned


class MandatoryPasswordChangeForm(forms.Form):
    new_password1 = forms.CharField(
        label="Nouveau mot de passe",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        min_length=14,
    )
    new_password2 = forms.CharField(
        label="Confirmation",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        min_length=14,
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("new_password1")
        p2 = cleaned.get("new_password2")
        if p1 and p2 and p1 != p2:
            self.add_error("new_password2", "Les mots de passe ne correspondent pas.")
        if p1 and self.user is not None:
            from django.contrib.auth.password_validation import validate_password

            try:
                validate_password(p1, user=self.user)
            except forms.ValidationError as exc:
                self.add_error("new_password1", exc)
            except Exception as exc:  # django.core.exceptions.ValidationError
                from django.core.exceptions import ValidationError as DjangoValidationError

                if isinstance(exc, DjangoValidationError):
                    self.add_error("new_password1", exc)
                else:
                    raise
        return cleaned


class InviteRedeemForm(MandatoryPasswordChangeForm):
    token = forms.CharField(label="Jeton d’invitation", max_length=200, widget=forms.TextInput())


class AccountCreateForm(forms.Form):
    """Création rapide : rôle → confirmation → pseudonyme et secret auto."""

    role = forms.ChoiceField(
        label="Rôle",
        choices=[
            ("editor", "Éditeur / utilisateur"),
            ("admin", "Administrateur (entrée privée uniquement)"),
            ("reviewer", "Relecteur"),
            ("operator", "Opérateur"),
            ("collector", "Collecte"),
        ],
        initial="editor",
    )
    access_mode = forms.ChoiceField(
        label="Accès initial",
        choices=[
            ("temp_password", "Secret initial fort (changement obligatoire au 1er accès)"),
            ("invite", "Jeton d’invitation à usage unique"),
        ],
        initial="temp_password",
        help_text="Aucun choix manuel d’identifiant : le pseudonyme est généré automatiquement.",
    )
    confirm_create = forms.BooleanField(
        label="Je confirme la création (pseudonyme auto + secret initial affiché une seule fois)",
        required=True,
    )
    is_test_identity = forms.BooleanField(
        label="Identité de test (jamais revue réelle)",
        required=False,
        initial=False,
    )
