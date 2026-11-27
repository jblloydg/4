from captcha.models import CaptchaStore

from core.models import Category, ItemPost, QuestionTemplate, SystemSettings

from .base import FindItTestCase, photo


class PostTests(FindItTestCase):
    def test_photo_and_category_required_title_optional(self):
        client = self.client_for(self.alice)
        r = client.post("/posts/new/", {"post_type": "LOST", "category": self.category.pk})
        self.assertContains(r, "Add at least one photo")
        r = client.post("/posts/new/", {"post_type": "LOST", "photos": [photo()]})
        self.assertEqual(r.status_code, 200)
        post = self.create_post(self.alice, "LOST")
        self.assertEqual(post.title, "")
        self.assertEqual(post.display_title, "Lost keys")
        self.assertEqual(post.photos.count(), 1)

    def test_more_than_ten_photos_needs_captcha_and_max_twenty(self):
        client = self.client_for(self.alice)
        base = {"post_type": "LOST", "category": self.category.pk}
        r = client.post("/posts/new/", {**base, "photos": [photo(f"{i}.jpg") for i in range(11)]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("captcha", r.context["form"].errors)
        r = client.post("/posts/new/", {**base, "photos": [photo(f"{i}.jpg") for i in range(11)],
                                        "captcha_0": "x", "captcha_1": "wrong"})
        self.assertIn("captcha", r.context["form"].errors)
        key = CaptchaStore.generate_key()
        answer = CaptchaStore.objects.get(hashkey=key).response
        r = client.post("/posts/new/", {**base, "photos": [photo(f"{i}.jpg") for i in range(11)],
                                        "captcha_0": key, "captcha_1": answer})
        self.assertEqual(r.status_code, 302)
        r = client.post("/posts/new/", {**base, "photos": [photo(f"{i}.jpg") for i in range(21)]})
        self.assertContains(r, "at most 20 photos")

    def test_posts_per_hour_is_admin_adjustable(self):
        settings_row = SystemSettings.current()
        settings_row.posts_per_hour = 2
        settings_row.save()
        self.create_post(self.alice, "LOST")
        self.create_post(self.alice, "LOST")
        r = self.client_for(self.alice).post("/posts/new/", {"post_type": "LOST", "category": self.category.pk, "photos": [photo()]})
        self.assertEqual(ItemPost.objects.filter(author=self.alice).count(), 2)
        self.assertRedirects(r, "/my-posts/", fetch_redirect_response=False)

    def test_category_filter_and_admin_only_category_page(self):
        self.create_post(self.alice, "LOST")
        r = self.client_for(self.alice).get(f"/home/?category={self.category.pk}")
        self.assertEqual(len(r.context["posts"]), 1)
        self.assertEqual(self.client_for(self.staff).get("/staff/categories/").status_code, 403)
        r = self.client_for(self.admin).post("/staff/categories/", {"name": "Lunch boxes", "sort_order": 5, "is_active": "on"})
        self.assertTrue(Category.objects.filter(name="Lunch boxes").exists())


class QuestionTests(FindItTestCase):
    def test_defaults_fill_to_three_and_max_five(self):
        post = self.create_post()
        self.assertEqual(post.verification_questions.count(), 3)
        self.assertTrue(all(q.template.is_default for q in post.verification_questions.all()))
        brand = QuestionTemplate.objects.get(text="How many keys are on it?")
        post = self.create_post(questions=[brand])
        texts = [q.text for q in post.verification_questions.all()]
        self.assertEqual(len(texts), 3)
        self.assertEqual(texts[0], brand.text)
        six = list(QuestionTemplate.objects.all()[:6])
        r = self.client_for(self.finder).post("/posts/new/", {"post_type": "FOUND", "category": self.category.pk,
                                                              "photos": [photo()], "questions": [q.pk for q in six]})
        self.assertContains(r, "at most 5")

    def test_claim_requires_description_and_valid_choice_and_snapshot_kept(self):
        post = self.create_post()
        choice_q = post.verification_questions.filter(answer_type="CHOICE").first()
        self.claim(post, self.alice, description="")
        self.assertEqual(post.claims.count(), 0)
        data = {f"answer_{q.pk}": (q.choices[0] if q.choices else "x") for q in post.verification_questions.all()}
        data[f"answer_{choice_q.pk}"] = "Not an option"
        data["description"] = "mine"
        self.client_for(self.alice).post(f"/posts/{post.pk}/claim/", data)
        self.assertEqual(post.claims.count(), 0)
        self.claim(post, self.alice, description="x")  # no minimum length
        self.assertEqual(post.claims.count(), 1)
        template = choice_q.template
        template.text = "Changed wording"
        template.save()
        choice_q.refresh_from_db()
        self.assertNotEqual(choice_q.text, "Changed wording")

    def test_admin_question_bank_keeps_three_defaults(self):
        default = QuestionTemplate.objects.filter(is_default=True).first()
        client = self.client_for(self.admin)
        r = client.post(f"/staff/questions/{default.pk}/", {
            "text": default.text, "answer_type": default.answer_type, "choices_text": "\n".join(default.choices),
            "is_general": "on", "is_active": "on", "sort_order": 1,
        })
        self.assertContains(r, "Keep at least 3")
        self.assertEqual(self.client_for(self.staff).get("/staff/questions/").status_code, 403)
