import numpy as np
import numpy.random as npr
import chainer as ch

from tagger import extract_all_mentions
from special_functions import batch_weighted_softmax_cross_entropy
from gru import GRU, BidirectionalGRU

class Extractor(ch.Chain):
    def __init__(self,
                 tagger,
                 n_mention_class,
                 n_relation_class,
                 lstm_size=50,
                 bidirectional=False,
                 n_layers=1,
                 use_mlp=False,
                 dropout=.25,
                 start_tags=(2,),
                 in_tags=(1,2),
                 out_tags=(0,),
                 tag2mtype=None,
                 mtype2msubtype=None,
                 msubtype2rtype=None,
                 max_rel_dist=10000):
        # setup rnn layer
        if bidirectional:
            feature_size = 2*lstm_size
            lstms = [BidirectionalGRU(lstm_size, n_inputs=tagger.feature_size)]
            for i in range(1,n_layers):
                lstms.append(BidirectionalGRU(lstm_size, n_inputs=feature_size))
        else:
            feature_size = lstm_size
            lstms = [GRU(lstm_size, n_inputs=tagger.feature_size)]
            for i in range(1,n_layers):
                lstms.append(GRU(lstm_size, n_inputs=feature_size))
        # setup other links
        super(Extractor, self).__init__(
            tagger=tagger,
            mlp = ch.links.Linear(feature_size, feature_size),
            out=ch.links.Linear(feature_size, feature_size),
            f_m=ch.links.Linear(feature_size, n_mention_class),
            f_r=ch.links.Linear(2*feature_size, n_relation_class)
        )
        self.lstms = lstms
        for i, lstm in enumerate(self.lstms):
            self.add_link('lstm_{}'.format(i), lstm)
        self.lstm_size = lstm_size
        self.feature_size = feature_size
        self.bidirectional = bidirectional
        self.dropout = dropout
        self.use_mlp = use_mlp
        self.n_layers = n_layers
        self.start_tags = start_tags
        self.in_tags = in_tags
        self.out_tags = out_tags
        self.tag2mtype = tag2mtype
        self.max_rel_dist = max_rel_dist

        # convert the typemaps to indicator array masks
        for k,v in mtype2msubtype.items():
            mask = np.zeros(n_mention_class).astype(np.float32)
            mask[np.array(v)] = 1.
            mtype2msubtype[k] = mask
        self.mtype2msubtype = mtype2msubtype
        for k,v in msubtype2rtype['left'].items():
            mask = np.zeros(n_relation_class).astype(np.float32)
            mask[np.array(v)] = 1.
            msubtype2rtype['left'][k] = mask
        for k,v in msubtype2rtype['right'].items():
            mask = np.zeros(n_relation_class).astype(np.float32)
            mask[np.array(v)] = 1.
            msubtype2rtype['right'][k] = mask
        self.msubtype2rtype = msubtype2rtype
        # print self.mtype2msubtype
        # print
        # print self.msubtype2rtype

    def reset_state(self):
        self.tagger.reset_state()
        for lstm in self.lstms:
            lstm.reset_state()

    def _extract_graph(self, tagger_preds, features):
        """ Subroutine responsible for extracting the graph and graph features
        from the tagger predictions using `extract_all_mentions`.

        Note: This function can be slow for large documents.
          This is unavoidable for documents with large mention counts (m)
          because the number of relations r is naively (m choose 2).
          This graph can be pruned by setting `max_rel_dist`,
          which will omit all relations for mentions `max_rel_dist` apart
          (as measured from the left edge of the consituent mention spans.)
        """
        # TODO: Possible extension: using separate features for mentions and relations

        # convert from time-major to batch-major
        tagger_preds = ch.functions.transpose_sequence(tagger_preds)
        # for p in tagger_preds:
            # print p.shape, p.data
        features = ch.functions.transpose_sequence(features)

        # extract the mentions and relations for each doc
        all_boundaries = extract_all_mentions(tagger_preds,
                                              start_tags=self.start_tags,
                                              in_tags=self.in_tags,
                                              out_tags=self.out_tags,
                                              tag2mtype=self.tag2mtype)

        all_mentions = [] # features for mentions
        all_mention_spans = [] # spans in doc for each mention
        all_mention_masks = [] # bool masks for constraining predictions
        all_left_mentions = [] # features for left mention of a relation
        all_right_mentions = [] # features for right mention of a relation
        all_relation_spans = [] # spans in doc for left and right mentions
        all_left_mention_masks = [] # bool mask for constraining predictions
        all_right_mention_masks = [] # bool mask for constraining predictions
        zipped = zip(all_boundaries, tagger_preds, features)
        # extract graph and features for each doc
        for s, (boundaries, seq, features) in enumerate(zipped):
            mentions = []
            mention_spans = []
            mention_masks = []
            left_mentions = []
            right_mentions = []
            relation_spans = []
            left_mention_masks = []
            right_mention_masks = []
            moving_rel_idx = 0
            for i, b in enumerate(boundaries):
                # mention feature is average of its span features
                mention = ch.functions.sum(features[b[0]:b[1]], axis=0)
                mention /= ch.functions.broadcast_to(ch.Variable(np.array(b[1]-b[0],
                                                             dtype=np.float32)),
                                                 mention.shape)
                mentions.append(mention)
                mention_spans.append((b[0], b[1]))
                mention_masks.append(self.mtype2msubtype[b[2]])
                # make a relation to all previous mentions (M choose 2)
                # except those that are further than max_rel_dist away
                # (prune for speed, accuracy)
                # for j in range(moving_rel_idx, i):
                #     bj = boundaries[j]
                #     if abs(bj[0] - b[0]) < self.max_rel_dist:
                #         relation_spans.append((bj[0], bj[1], b[0], b[1]))
                #         left_mentions.append(mentions[j])
                #         right_mentions.append(mentions[i])
                #         left_mention_masks.append(self.msubtype2rtype['left'][bj[2]])
                #         right_mention_masks.append(self.msubtype2rtype['right'][b[2]])
                #     # if that's too far, then it'll def be too far for the next
                #     else:
                #         moving_rel_idx = j
            if mentions:
                mentions = ch.functions.vstack(mentions)
                mention_masks = ch.functions.vstack(mention_masks)
            all_mentions.append(mentions)
            all_mention_masks.append(mention_masks)
            all_mention_spans.append(mention_spans)

            if left_mentions:
                left_mentions = ch.functions.vstack(left_mentions)
                left_mention_masks = ch.functions.vstack(left_mention_masks)
            all_left_mentions.append(left_mentions)
            all_left_mention_masks.append(left_mention_masks)
            if right_mentions:
                right_mentions = ch.functions.vstack(right_mentions)
                right_mention_masks = ch.functions.vstack(right_mention_masks)
            all_right_mentions.append(right_mentions)
            all_right_mention_masks.append(right_mention_masks)
            all_relation_spans.append(relation_spans)

        return (all_mentions, all_left_mentions, all_right_mentions,
                all_mention_masks, all_left_mention_masks, all_right_mention_masks,
                all_mention_spans, all_relation_spans)

    def __call__(self, x_list, train=True, backprop_to_tagger=False):
        drop = ch.functions.dropout
        # first tag the doc
        tagger_preds, tagger_features = self.tagger.predict(x_list,
                                                            return_features=True)

        if not backprop_to_tagger:
            for feature in tagger_features:
                feature.unchain_backward()

        # skip the tagger features and go from embeddings
        # tagger_features = [drop(self.tagger.embed(x), self.dropout, train) for x in x_list]

        # do another layer of features on the tagger layer
        # features = [ self.lstm(f) for f in tagger_features ]
        if self.bidirectional:
            # helper function
            def bilstm(inputs, lstm):
                f_lstms, b_lstms = [], []
                for x_f, x_b in zip(inputs, inputs[::-1]):
                    h_f, h_b = lstm(x_f, x_b)
                    f_lstms.append(h_f)
                    b_lstms.append(h_b)
                b_lstms = b_lstms[::-1]
                return [ ch.functions.hstack([f,b]) for f,b in zip(f_lstms, b_lstms)]
            # run the layers of bilstms
            lstms = [ drop(h, self.dropout, train) for h in bilstm(tagger_features, self.lstms[0]) ]
            for lstm in self.lstms[1:]:
                lstms = [ drop(h, self.dropout, train) for h in bilstm(lstms, lstm) ]
        else:
            lstms = [ drop(self.lstms[0](x), self.dropout, train) for x in tagger_features ]
            for lstm in self.lstms[1:]:
                lstms = [ drop(lstm(h), self.dropout, train) for h in lstms ]

        f = ch.functions.leaky_relu
        # rnn output layer
        lstms = [ drop(f(self.out(h)) , self.dropout, train) for h in lstms ]

        # hidden layer
        if self.use_mlp:
            lstms = [ drop(f(self.mlp(h)) , self.dropout, train) for h in lstms ]

        # extract the information graph from the tagger
        (mentions, l_mentions, r_mentions,
         mention_masks, l_mention_masks, r_mention_masks,
         m_spans, r_spans) = self._extract_graph( tagger_preds,lstms)
        # print [m.shape for m in mentions]
        # concat left and right mentions into one vector per relation
        relations = [ ch.functions.concat(m, axis=1)
                      if type(m[0]) is ch.Variable else m[0] # make sure its nonempty
                      for m in zip(l_mentions, r_mentions) ]

        # score mentions and relations
        # additionally apply any type constraint masks
        m_logits = [ mask * self.f_m(m) if type(m) is ch.Variable else []
                     for m, mask in zip(mentions, mention_masks) ]
        r_logits = [ l_mask * r_mask * self.f_r(r) if type(r) is ch.Variable else []
                     for r, l_mask, r_mask
                     in zip(relations, l_mention_masks, r_mention_masks) ]
        return m_logits, r_logits, m_spans, r_spans

    def predict(self, x_list, reset_state=True):
        if reset_state:
            self.reset_state()
        m_logits, r_logits, m_spans, r_spans = self(x_list)
        m_preds = [ ch.functions.argmax(m, axis=1).data
                    if type(m) is ch.Variable else []
                    for m in m_logits ]
        r_preds = [ ch.functions.argmax(r, axis=1).data
                    if type(r) is ch.Variable else []
                    for r in r_logits ]
        return m_preds, r_preds, m_spans, r_spans

class ExtractorLoss(ch.Chain):
    def __init__(self, extractor):
        super(ExtractorLoss, self).__init__(
            extractor=extractor
        )

    def __call__(self, x_list, gold_m_list, gold_r_list, **kwds):
        # extract the graph
        men_logits, rel_logits, men_spans, rel_spans = self.extractor(x_list, **kwds)
        # print zip([len(m) for m in men_logits], [len(r) for r in rel_logits])
        # compute loss per sequence
        mention_loss = relation_loss = 0
        batch_size = float(len(men_logits))
        zipped = zip(men_logits, rel_logits,
                     men_spans, gold_m_list,
                     rel_spans, gold_r_list)
        for (m_logits, r_logits, m_spans, gold_m, r_spans, gold_r) in zipped:
            # using gold mentions, construct a matching label and truth array
            # that indicates if a mention boundary prediction is correct
            # and if so the index of the correct mention type (or 0 if not)
            # So the type loss is only calculated for correctly detected mentions.
            #
            # To prevent degenerate solutions that force the tagger to not output
            # as many correct mentions (resulting in trivially lower loss),
            # we rescale the loss by (# true mentions / # correct mentions).
            # Intuitively this creates a higher losses for less correct mentions
            gold_spans = {m[:2] for m in gold_m}
            span2label = {m[:2]:m[2] for m in gold_m}
            weights = []
            labels = []
            for m in m_spans:
                if m in gold_spans:
                    weights.append(1.0)
                    labels.append(span2label[m])
                else:
                    weights.append(0.0)
                    labels.append(0)
            weights = np.array(weights, dtype=np.float32)
            # print '-'*80
            # print "{} true, {} pred, {} correct mentions".format(
            #   len(gold_spans), len(m_spans), np.sum(weights))
            # print len(gold_m), gold_m
            # print len(m_spans), m_spans
            labels = np.array(labels, dtype=np.int32)
            # print type(m_logits), len(m_logits)
            doc_mention_loss = batch_weighted_softmax_cross_entropy(m_logits, labels,
                                                                    instance_weight=weights)
            doc_mention_loss *= len(weights) / (np.sum(weights) + 1e-15)
            mention_loss += doc_mention_loss

            # do the same for relations
            # but only if BOTH mention boundaries are correct
            # gold_rel_spans = set([r[:4] for r in gold_r])
            # rel2label = {r[:4]:r[4] for r in gold_r}
            # weights = []
            # labels = []
            # for r in r_spans:
            #     # NOTE the following commented out conditional is buggy,
            #     # but it should not be...
            #     # there should be no relations whose spans are not gold mentions
            #     if (r[:2] in gold_spans) and (r[2:4] in gold_spans):
            #     # if r in gold_rel_spans:
            #         weights.append(1.0)
            #         labels.append(rel2label[r[:4]])
            #     else:
            #         weights.append(0.0)
            #         labels.append(0)
            # weights = np.array(weights, dtype=np.float32)
            # labels = np.array(labels, dtype=np.int32)
            # relation_loss += batch_weighted_softmax_cross_entropy(r_logits, labels,
            #                                                      instance_weight=weights)
        mention_loss /= batch_size
        # relation_loss /= batch_size
        return (mention_loss)# + relation_loss)
